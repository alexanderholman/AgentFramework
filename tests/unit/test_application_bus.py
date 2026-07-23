from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from af.framework.application_bus import (
    ApplicationBus,
    ApplicationResult,
    AuthorizationMiddleware,
    AuthorizationRegistry,
    Command,
    ContextMiddleware,
    FailureSanitizationMiddleware,
    Gate,
    GateDenied,
    GateEvidence,
    GateReason,
    GateRequirement,
    HandlerRegistry,
    InProcessApplicationAdapter,
    PolicyOutcome,
    Provenance,
    ProvenanceRef,
    Query,
    TracingMiddleware,
    TransactionMiddleware,
    ValidationMiddleware,
    ValidationRegistry,
)
from af.framework.context import (
    AcceptanceScope,
    ExecutionContext,
    Principal,
    current_context,
)
from af.framework.errors import PlatformError, PlatformReason

_NOW = datetime(2026, 7, 23, 21, tzinfo=UTC)
_DIGEST = "a" * 64


@dataclass(frozen=True, slots=True)
class _ReadWidget(Query):
    tenant_id: str
    resource_fingerprint: str


@dataclass(frozen=True, slots=True)
class _CreateWidget(Command):
    tenant_id: str
    name: str


class _AllowPolicy:
    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events

    def decide(self, principal, requirement, context):
        if self._events is not None:
            self._events.append("policy")
        assert principal is context.principal
        assert requirement.resource_fingerprint == _DIGEST
        return PolicyOutcome(True, (GateEvidence("policy.owner_claim"),))


class _DenyPolicy:
    def decide(self, principal, requirement, context):
        del principal, requirement, context
        return PolicyOutcome(False, (GateEvidence("policy.rule_denied"),))


class _TraceSink:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


class _RecordingMiddleware:
    def __init__(self, name: str, events: list[str]) -> None:
        self._name = name
        self._events = events

    async def __call__(self, message, context, call_next):
        self._events.append(f"{self._name}.before")
        result = await call_next(message, context)
        self._events.append(f"{self._name}.after")
        return result


class _Transaction:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self) -> None:
        self._events.append("transaction.enter")

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self._events.append("transaction.exit")


def _context(
    *,
    tenant_id: str = "tenant-a",
    scopes: tuple[str, ...] = ("widgets.read",),
) -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(
            subject_id="subject-a",
            principal_type="user",
            scopes=scopes,
        ),
        tenant_id=tenant_id,
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )


def _provenance() -> Provenance:
    return Provenance(
        use_case="widgets.read",
        handler_id="widgets.read-handler",
        handler_version="1.0.0",
        occurred_at=_NOW,
        sources=(ProvenanceRef("request", _DIGEST, "1.0.0"),),
        derived_from=(ProvenanceRef("widget", "b" * 64),),
    )


def _requirement(message, context) -> GateRequirement:
    del context
    return GateRequirement(
        action="read",
        resource_type="widget",
        tenant_id=message.tenant_id,
        required_scopes=("widgets.read",),
        required_acceptance_scope=AcceptanceScope.DETERMINISTIC,
        resource_fingerprint=message.resource_fingerprint,
    )


def _authorized_bus(handler, *, policy=None, middleware=()):
    handlers = HandlerRegistry()
    handlers.register(_ReadWidget, handler)
    authorization = AuthorizationRegistry()
    authorization.register(_ReadWidget, _requirement)
    gate = Gate(policy_version="gate-v1")
    gate.register("widget", "read", policy or _AllowPolicy(), policy_version="widgets-v2")
    return ApplicationBus(
        handlers,
        middleware=(
            FailureSanitizationMiddleware(),
            ContextMiddleware(),
            AuthorizationMiddleware(gate, authorization),
            *middleware,
        ),
    )


def test_direct_dispatch_and_in_process_adapter_have_equivalent_semantics() -> None:
    async def handler(message, context):
        assert current_context() is context
        return ApplicationResult(f"{message.tenant_id}:widget", _provenance())

    bus = _authorized_bus(handler)
    message = _ReadWidget("tenant-a", _DIGEST)

    direct = asyncio.run(bus.dispatch(message, _context()))
    adapted = asyncio.run(InProcessApplicationAdapter(bus).send(message, _context()))

    assert direct == adapted
    assert direct.value == "tenant-a:widget"
    assert direct.provenance.authorization is not None
    assert direct.provenance.authorization.allowed
    assert direct.provenance.authorization.policy_version == "widgets-v2"
    assert direct.provenance.authorization.evidence == (GateEvidence("policy.owner_claim"),)


def test_middleware_and_transaction_cleanup_order_is_deterministic() -> None:
    events: list[str] = []
    handlers = HandlerRegistry()

    async def handler(_message, _context):
        events.append("handler")
        return ApplicationResult("ok", _provenance())

    handlers.register(_ReadWidget, handler)
    bus = ApplicationBus(
        handlers,
        middleware=(
            _RecordingMiddleware("outer", events),
            _RecordingMiddleware("inner", events),
            TransactionMiddleware(lambda _message, _context: _Transaction(events)),
        ),
    )

    assert asyncio.run(bus.dispatch(_ReadWidget("tenant-a", _DIGEST), _context())).value == "ok"
    assert events == [
        "outer.before",
        "inner.before",
        "transaction.enter",
        "handler",
        "transaction.exit",
        "inner.after",
        "outer.after",
    ]


def test_default_deny_happens_before_handler_or_resource_candidate_access() -> None:
    accessed: list[str] = []
    handlers = HandlerRegistry()

    async def handler(_message, _context):
        accessed.append("repository")
        return ApplicationResult("secret-widget", _provenance())

    handlers.register(_ReadWidget, handler)
    bus = ApplicationBus(
        handlers,
        middleware=(AuthorizationMiddleware(Gate(policy_version="gate-v1"), AuthorizationRegistry()),),
    )

    with pytest.raises(GateDenied) as captured:
        asyncio.run(bus.dispatch(_ReadWidget("tenant-a", _DIGEST), _context()))

    assert accessed == []
    assert captured.value.decision.reason is GateReason.DEFAULT_DENY
    assert str(captured.value) == "operation is not authorized"
    assert "widget" not in str(captured.value)


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (_context(tenant_id="tenant-b"), GateReason.TENANT_DENIED),
        (_context(scopes=()), GateReason.SCOPE_DENIED),
        (
            ExecutionContext(
                principal=Principal("subject-a", "user", scopes=("widgets.read",)),
                tenant_id="tenant-a",
                effective_scope=AcceptanceScope.REVIEWED_GENERATIVE,
            ),
            GateReason.ACCEPTANCE_SCOPE_DENIED,
        ),
    ],
)
def test_tenant_resource_and_acceptance_scope_checks_precede_policy(context, reason) -> None:
    events: list[str] = []
    gate = Gate(policy_version="gate-v1")
    gate.register("widget", "read", _AllowPolicy(events))

    decision = gate.evaluate(context, _requirement(_ReadWidget("tenant-a", _DIGEST), context))

    assert not decision.allowed
    assert decision.reason is reason
    assert events == []


def test_policy_denial_retains_fixed_version_reason_and_safe_evidence() -> None:
    gate = Gate(policy_version="gate-v1")
    gate.register("widget", "read", _DenyPolicy(), policy_version="widgets-v9")

    decision = gate.evaluate(
        _context(),
        _requirement(_ReadWidget("tenant-a", _DIGEST), _context()),
    )

    assert decision == type(decision)(
        allowed=False,
        reason=GateReason.POLICY_DENIED,
        policy_version="widgets-v9",
        evidence=(GateEvidence("policy.rule_denied"),),
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )


def test_validation_short_circuits_authorization_and_handler_with_sanitized_failure() -> None:
    events: list[str] = []
    handlers = HandlerRegistry()

    async def handler(_message, _context):
        events.append("handler")
        return ApplicationResult("ok", _provenance())

    async def validator(_message, _context):
        events.append("validate")
        raise ValueError("private-input-detail")

    handlers.register(_ReadWidget, handler)
    validators = ValidationRegistry()
    validators.register(_ReadWidget, validator)
    authorization = AuthorizationRegistry()
    authorization.register(_ReadWidget, _requirement)
    gate = Gate(policy_version="gate-v1")
    gate.register("widget", "read", _AllowPolicy(events))
    bus = ApplicationBus(
        handlers,
        middleware=(
            FailureSanitizationMiddleware(),
            ValidationMiddleware(validators),
            AuthorizationMiddleware(gate, authorization),
        ),
    )

    with pytest.raises(PlatformError) as captured:
        asyncio.run(bus.dispatch(_ReadWidget("tenant-a", _DIGEST), _context()))

    assert captured.value.reason is PlatformReason.VALIDATION_FAILED
    assert "private-input-detail" not in str(captured.value)
    assert events == ["validate"]


def test_unknown_handler_failure_is_sanitized_but_retained_as_protected_cause() -> None:
    handlers = HandlerRegistry()

    async def handler(_message, _context):
        raise RuntimeError("provider-secret")

    handlers.register(_ReadWidget, handler)
    bus = ApplicationBus(handlers, middleware=(FailureSanitizationMiddleware(),))

    with pytest.raises(PlatformError) as captured:
        asyncio.run(bus.dispatch(_ReadWidget("tenant-a", _DIGEST), _context()))

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert isinstance(captured.value.protected_cause, RuntimeError)
    assert "provider-secret" not in str(captured.value)


def test_cancellation_propagates_and_transaction_cleanup_completes() -> None:
    events: list[str] = []
    handlers = HandlerRegistry()
    context = _context()

    async def handler(_message, current):
        events.append("handler")
        current.cancellation.cancel()
        return ApplicationResult("ignored", _provenance())

    handlers.register(_ReadWidget, handler)
    bus = ApplicationBus(
        handlers,
        middleware=(TransactionMiddleware(lambda _message, _context: _Transaction(events)),),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bus.dispatch(_ReadWidget("tenant-a", _DIGEST), context))

    assert events == ["transaction.enter", "handler", "transaction.exit"]


def test_pre_cancelled_context_never_enters_middleware_or_handler() -> None:
    events: list[str] = []
    handlers = HandlerRegistry()
    context = _context()
    context.cancellation.cancel()

    async def handler(_message, _context):
        events.append("handler")
        return ApplicationResult("ignored", _provenance())

    handlers.register(_ReadWidget, handler)
    bus = ApplicationBus(handlers, middleware=(_RecordingMiddleware("middleware", events),))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bus.dispatch(_ReadWidget("tenant-a", _DIGEST), context))

    assert events == []


def test_tracing_records_success_failure_and_cancellation_without_changing_semantics() -> None:
    sink = _TraceSink()
    handlers = HandlerRegistry()

    async def handler(_message, _context):
        return ApplicationResult("ok", _provenance())

    handlers.register(_ReadWidget, handler)
    bus = ApplicationBus(
        handlers,
        middleware=(TracingMiddleware(sink, clock=lambda: _NOW),),
    )
    asyncio.run(bus.dispatch(_ReadWidget("tenant-a", _DIGEST), _context()))

    assert [event.stage for event in sink.events] == ["started", "completed"]
    assert all(event.occurred_at == _NOW for event in sink.events)


def test_registry_is_exact_type_and_rejects_duplicate_or_missing_handlers() -> None:
    handlers = HandlerRegistry()

    async def handler(_message, _context):
        return ApplicationResult("ok", _provenance())

    handlers.register(_ReadWidget, handler)
    with pytest.raises(ValueError):
        handlers.register(_ReadWidget, handler)

    bus = ApplicationBus(handlers)
    with pytest.raises(PlatformError) as captured:
        asyncio.run(bus.dispatch(_CreateWidget("tenant-a", "name"), _context()))
    assert captured.value.reason is PlatformReason.DEPENDENCY_RESOLUTION_FAILED


def test_invalid_policy_outcome_and_policy_exception_fail_closed() -> None:
    class InvalidPolicy:
        def decide(self, principal, requirement, context):
            del principal, requirement, context
            return object()

    class FailingPolicy:
        def decide(self, principal, requirement, context):
            del principal, requirement, context
            raise RuntimeError("resource-exists-secret")

    for policy, evidence in (
        (InvalidPolicy(), GateEvidence("policy.invalid_outcome")),
        (FailingPolicy(), GateEvidence("policy.failure")),
    ):
        gate = Gate(policy_version="gate-v1")
        gate.register("widget", "read", policy)
        decision = gate.evaluate(
            _context(),
            _requirement(_ReadWidget("tenant-a", _DIGEST), _context()),
        )
        assert not decision.allowed
        assert decision.reason is GateReason.POLICY_DENIED
        assert decision.evidence == (evidence,)


def test_provenance_rejects_raw_or_malformed_identity_and_conflicting_authorization() -> None:
    with pytest.raises(ValueError):
        ProvenanceRef("widget", "raw-resource-id")
    with pytest.raises(ValueError):
        Provenance(
            use_case="bad use case",
            handler_id="handler",
            handler_version="1.0.0",
            occurred_at=_NOW,
        )

    gate = Gate(policy_version="gate-v1")
    gate.register("widget", "read", _AllowPolicy())
    decision = gate.authorize(
        _context(),
        _requirement(_ReadWidget("tenant-a", _DIGEST), _context()),
    )
    result = ApplicationResult("ok", _provenance()).authorized_by(decision)
    assert result.provenance.authorization is decision

    other = GateDecision(
        allowed=True,
        reason=GateReason.ALLOWED,
        policy_version="different-v1",
        evidence=(),
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )
    with pytest.raises(ValueError):
        result.authorized_by(other)
