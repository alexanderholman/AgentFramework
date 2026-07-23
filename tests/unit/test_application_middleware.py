from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import af.application.middleware as application_middleware
from af.application.bus import (
    ApplicationBus,
    ApplicationResult,
    Command,
    Gate,
    GateDecision,
    GateEffect,
    GateMiddleware,
    HandlerRegistry,
    PolicyEvidence,
    Query,
    SanitizedFailureMiddleware,
)
from af.application.middleware import (
    InProcessTestAdapter,
    ScopedAuthorizationRequest,
    TenantScopePolicy,
    TracingMiddleware,
    TransactionMiddleware,
    ValidationMiddleware,
)
from af.framework.context import AcceptanceScope, ExecutionContext, Principal
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import (
    CapabilityEvidence,
    CapabilitySet,
    DriverManifest,
    HealthStatus,
    Manager,
)

_NOW = datetime(2026, 7, 23, 21, tzinfo=UTC)
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64


@dataclass(frozen=True, slots=True)
class _ReadWidget(Query):
    tenant_id: str
    resource_id: str


@dataclass(frozen=True, slots=True)
class _WriteWidget(Command):
    tenant_id: str
    value: str


class _Transaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> None:
        self._events.append("transaction.enter")

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self._events.append("transaction.exit")


class _TransactionDriver:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def transaction(self, message, context):
        assert message.tenant_id == context.tenant_id
        return _Transaction(self._events)


class _TraceSink:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


def _context(
    *,
    tenant_id: str = "tenant-a",
    scopes: tuple[str, ...] = ("widgets.read",),
    principal: bool = True,
) -> ExecutionContext:
    return ExecutionContext(
        principal=(
            Principal(
                subject_id="subject-a",
                principal_type="user",
                scopes=scopes,
            )
            if principal
            else None
        ),
        tenant_id=tenant_id,
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )


def _transaction_manager(events: list[str]) -> Manager:
    manager = Manager(
        manager_id="transactions",
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manifest = DriverManifest(
        driver_name="memory",
        version="1.0.0",
        artifact_digest=_DIGEST_A,
        dependency_graph_digest=_DIGEST_B,
        capabilities=CapabilitySet(("transactions",)),
    )

    def verifier(_ref, _driver, binding):
        return CapabilityEvidence(
            binding=binding,
            capabilities=CapabilitySet(("transactions",)),
            health=HealthStatus.HEALTHY,
            observed_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
            evidence_digest=_DIGEST_C,
        )

    manager.extend(
        "memory",
        lambda _ref, _config: _TransactionDriver(events),
        manifest=manifest,
        verifier=verifier,
    )
    return manager


def _allow(_request, _context):
    return GateDecision(
        effect=GateEffect.ALLOW,
        policy_version="policy-v1",
        reason_code="policy_allowed",
        evidence=(PolicyEvidence("role", "reader"),),
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )


def _authorization(message, _context):
    return ScopedAuthorizationRequest(
        action="read",
        resource_type="widget",
        protected_resource_id=message.resource_id,
        required_scope=AcceptanceScope.DETERMINISTIC,
        tenant_id=message.tenant_id,
        required_principal_scopes=("widgets.read",),
    )


def test_manager_backed_transaction_wraps_handler_and_cleans_up_deterministically() -> None:
    events: list[str] = []
    registry = HandlerRegistry()

    async def handler(message, _context):
        events.append(f"handler:{message.value}")
        return ApplicationResult("written")

    registry.register(_WriteWidget, handler)
    bus = ApplicationBus(
        registry,
        middleware=(TransactionMiddleware(_transaction_manager(events)),),
    )

    result = asyncio.run(bus.dispatch(_WriteWidget("tenant-a", "value"), context=_context()))

    assert result.value == "written"
    assert events == ["transaction.enter", "handler:value", "transaction.exit"]


def test_transaction_cleanup_completes_when_task_is_cancelled() -> None:
    events: list[str] = []
    registry = HandlerRegistry()

    async def handler(_message, _context):
        events.append("handler")
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        return ApplicationResult("unreachable")

    registry.register(_WriteWidget, handler)
    bus = ApplicationBus(
        registry,
        middleware=(TransactionMiddleware(_transaction_manager(events)),),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bus.dispatch(_WriteWidget("tenant-a", "value"), context=_context()))

    assert events == ["transaction.enter", "handler", "transaction.exit"]


def test_validation_short_circuits_transaction_and_handler_before_resource_access() -> None:
    events: list[str] = []
    registry = HandlerRegistry()

    async def handler(_message, _context):
        events.append("handler")
        return ApplicationResult("unreachable")

    async def validate(_message, _context):
        events.append("validate")
        raise ValueError("private malformed value")

    registry.register(_WriteWidget, handler)
    bus = ApplicationBus(
        registry,
        middleware=(
            SanitizedFailureMiddleware(),
            ValidationMiddleware(validate),
            TransactionMiddleware(_transaction_manager(events)),
        ),
    )

    with pytest.raises(PlatformError) as captured:
        asyncio.run(bus.dispatch(_WriteWidget("tenant-a", "value"), context=_context()))

    assert captured.value.reason is PlatformReason.VALIDATION_FAILED
    assert "private malformed value" not in str(captured.value)
    assert events == ["validate"]


@pytest.mark.parametrize(
    ("context", "reason_code"),
    [
        (_context(tenant_id="tenant-b"), "tenant_denied"),
        (_context(scopes=()), "principal_scope_denied"),
        (_context(principal=False), "principal_required"),
    ],
)
def test_tenant_and_principal_scope_policy_denies_before_handler(context, reason_code) -> None:
    calls: list[str] = []
    registry = HandlerRegistry()

    def handler(_message, _context):
        calls.append("repository")
        return ApplicationResult("private")

    registry.register(_ReadWidget, handler)
    gate = Gate(
        policy_version="policy-v1",
        policies=(TenantScopePolicy(policy_version="policy-v1"), _allow),
    )
    bus = ApplicationBus(
        registry,
        middleware=(GateMiddleware(gate, authorize=_authorization),),
    )

    with pytest.raises(PlatformError) as captured:
        asyncio.run(
            bus.dispatch(
                _ReadWidget("tenant-a", "does-not-exist-secret"),
                context=context,
            )
        )

    assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED
    assert "does-not-exist-secret" not in str(captured.value)
    assert reason_code in repr(captured.value.protected_cause) or isinstance(
        captured.value.protected_cause,
        PermissionError,
    )
    assert calls == []


def test_matching_tenant_and_scopes_retain_gate_evidence_on_result() -> None:
    registry = HandlerRegistry()
    registry.register(_ReadWidget, lambda message, _context: ApplicationResult(message.resource_id))
    gate = Gate(
        policy_version="policy-v1",
        policies=(TenantScopePolicy(policy_version="policy-v1"), _allow),
    )
    bus = ApplicationBus(
        registry,
        middleware=(GateMiddleware(gate, authorize=_authorization),),
    )

    result = asyncio.run(
        bus.dispatch(
            _ReadWidget("tenant-a", "known"),
            context=_context(),
        )
    )

    assert result.value == "known"
    assert result.gate_decision is not None
    assert result.gate_decision.effect is GateEffect.ALLOW
    assert result.gate_decision.policy_version == "policy-v1"
    assert result.gate_decision.evidence == (PolicyEvidence("role", "reader"),)


def test_direct_and_in_process_test_adapter_dispatch_are_semantically_equivalent() -> None:
    registry = HandlerRegistry()
    registry.register(
        _ReadWidget,
        lambda message, context: ApplicationResult(
            (message.resource_id, context.tenant_id),
        ),
    )
    bus = ApplicationBus(registry)
    adapter = InProcessTestAdapter(bus)
    message = _ReadWidget("tenant-a", "widget-a")

    async def scenario():
        direct = await bus.dispatch(message, context=_context())
        adapted = await adapter.dispatch(message, context=_context())
        return direct, adapted

    direct, adapted = asyncio.run(scenario())
    assert direct == adapted == ApplicationResult(("widget-a", "tenant-a"))


def test_tracing_records_success_failure_and_cancellation_without_changing_results() -> None:
    sink = _TraceSink()
    registry = HandlerRegistry()
    registry.register(_ReadWidget, lambda _message, _context: ApplicationResult("ok"))
    bus = ApplicationBus(
        registry,
        middleware=(TracingMiddleware(sink, clock=lambda: _NOW),),
    )

    assert (
        asyncio.run(bus.dispatch(_ReadWidget("tenant-a", "known"), context=_context())).value
        == "ok"
    )
    assert [event.stage for event in sink.events] == ["started", "completed"]
    assert [event.message_kind for event in sink.events] == ["query", "query"]
    assert all(event.occurred_at == _NOW for event in sink.events)

    sink.events.clear()
    cancelled_registry = HandlerRegistry()

    async def cancelled(_message, _context):
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)
        return ApplicationResult("unreachable")

    cancelled_registry.register(_ReadWidget, cancelled)
    cancelled_bus = ApplicationBus(
        cancelled_registry,
        middleware=(TracingMiddleware(sink, clock=lambda: _NOW),),
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancelled_bus.dispatch(_ReadWidget("tenant-a", "known"), context=_context()))
    assert [event.stage for event in sink.events] == ["started", "cancelled"]


def test_scoped_authorization_request_redacts_tenant_scopes_and_resource_identity() -> None:
    request = ScopedAuthorizationRequest(
        action="read",
        resource_type="widget",
        protected_resource_id="resource-secret",
        required_scope=AcceptanceScope.DETERMINISTIC,
        tenant_id="tenant-secret",
        required_principal_scopes=("widgets.read",),
    )

    rendered = repr(request)
    assert "resource-secret" not in rendered
    assert "tenant-secret" not in rendered
    assert "widgets.read" not in rendered


def test_application_middleware_module_has_no_transport_or_provider_sdk_imports() -> None:
    source = inspect.getsource(application_middleware)
    forbidden = (
        "fastapi",
        "strawberry",
        "graphql",
        "typer",
        "sqlalchemy",
        "httpx",
        "boto",
        "redis",
        "kafka",
    )
    assert all(name not in source for name in forbidden)
