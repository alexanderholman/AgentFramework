from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from af.application.bus import (
    ApplicationBus,
    ApplicationResult,
    AuthorizationRequest,
    Command,
    DerivationEvidence,
    DispatchError,
    DispatchRequest,
    Gate,
    GateDecision,
    GateEffect,
    GateMiddleware,
    HandlerRegistry,
    InvalidDispatchContract,
    MessageKind,
    PolicyEvidence,
    ProvenanceRecord,
    Query,
    SanitizedFailureMiddleware,
)
from af.framework.context import (
    AcceptanceScope,
    ExecutionContext,
    Principal,
    maybe_current_context,
)
from af.framework.errors import PlatformError, PlatformReason

_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


@dataclass(frozen=True, slots=True)
class _Increment(Command):
    value: int


@dataclass(frozen=True, slots=True)
class _Read(Query):
    key: str


def _context(name: str = "one") -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(
            subject_id=f"subject-{name}",
            principal_type="user",
            roles=("operator",),
        ),
        tenant_id=f"tenant-{name}",
        correlation_id=f"correlation-{name}",
        requested_scope=AcceptanceScope.DETERMINISTIC,
    )


def _result(value: object) -> ApplicationResult[object]:
    return ApplicationResult(value=value)


def test_registry_resolves_exact_command_and_query_handlers_and_freezes() -> None:
    registry = HandlerRegistry()
    observed: list[tuple[object, ExecutionContext]] = []

    async def increment(message, context):
        observed.append((message, context))
        return _result(message.value + 1)

    def read(message, context):
        observed.append((message, context))
        return _result(f"value:{message.key}")

    registry.register(_Increment, increment)
    registry.register(_Read, read)
    bus = ApplicationBus(registry)
    context = _context()

    async def scenario() -> None:
        assert await bus.dispatch(_Increment(2), context=context) == _result(3)
        assert await bus.dispatch(_Read("alpha"), context=context) == _result("value:alpha")

    asyncio.run(scenario())

    assert registry.message_types == (_Increment, _Read)
    assert registry.kind_for(_Increment) is MessageKind.COMMAND
    assert registry.kind_for(_Read) is MessageKind.QUERY
    assert [item[1] for item in observed] == [context, context]
    with pytest.raises(InvalidDispatchContract, match="frozen"):
        registry.register(_Increment, increment)


def test_registry_rejects_duplicate_mutable_non_message_and_non_callable_handlers() -> None:
    registry = HandlerRegistry()

    @dataclass(slots=True)
    class MutableCommand(Command):
        value: int

    class NotAMessage:
        pass

    with pytest.raises(InvalidDispatchContract, match="immutable"):
        registry.register(MutableCommand, lambda _message, _context: _result(None))
    with pytest.raises(InvalidDispatchContract, match="Command or Query"):
        registry.register(NotAMessage, lambda _message, _context: _result(None))
    with pytest.raises(InvalidDispatchContract, match="callable"):
        registry.register(_Increment, object())

    registry.register(_Increment, lambda _message, _context: _result(None))
    with pytest.raises(InvalidDispatchContract, match="already"):
        registry.register(_Increment, lambda _message, _context: _result(None))


def test_dispatch_rejects_unregistered_and_mutated_message_shapes() -> None:
    registry = HandlerRegistry()
    registry.register(_Increment, lambda message, _context: _result(message.value))
    bus = ApplicationBus(registry)

    @dataclass(frozen=True, slots=True)
    class Unregistered(Command):
        value: str

    async def scenario() -> None:
        with pytest.raises(DispatchError) as missing:
            await bus.dispatch(Unregistered("no"), context=_context())
        assert missing.value.reason is PlatformReason.DEPENDENCY_RESOLUTION_FAILED
        with pytest.raises(InvalidDispatchContract):
            await bus.dispatch(object(), context=_context())

    asyncio.run(scenario())


def test_middleware_order_short_circuit_and_cleanup_are_deterministic() -> None:
    events: list[str] = []
    registry = HandlerRegistry()

    async def handler(message, _context):
        events.append(f"handler:{message.value}")
        return _result(message.value + 1)

    registry.register(_Increment, handler)

    class Recorder:
        def __init__(self, name: str) -> None:
            self.name = name

        async def __call__(self, request, call_next):
            events.append(f"{self.name}:before")
            try:
                result = await call_next(request)
                events.append(f"{self.name}:after")
                return result
            finally:
                events.append(f"{self.name}:cleanup")

    bus = ApplicationBus(registry, middleware=(Recorder("first"), Recorder("second")))

    async def scenario() -> None:
        assert await bus.dispatch(_Increment(1), context=_context()) == _result(2)

    asyncio.run(scenario())
    assert events == [
        "first:before",
        "second:before",
        "handler:1",
        "second:after",
        "second:cleanup",
        "first:after",
        "first:cleanup",
    ]

    events.clear()

    async def short_circuit(request, _call_next):
        events.append(f"short:{request.kind}")
        return _result("stopped")

    stopped = ApplicationBus(registry, middleware=(short_circuit, Recorder("unreached")))

    async def stopped_scenario() -> None:
        assert await stopped.dispatch(_Increment(9), context=_context()) == _result("stopped")

    asyncio.run(stopped_scenario())
    assert events == ["short:command"]


def test_middleware_cannot_call_next_twice_or_change_dispatch_identity() -> None:
    registry = HandlerRegistry()
    registry.register(_Increment, lambda message, _context: _result(message.value))

    async def twice(request, call_next):
        await call_next(request)
        return await call_next(request)

    async def replace_message(request, call_next):
        return await call_next(
            type(request)(
                message=_Increment(99),
                kind=request.kind,
                context=request.context,
            )
        )

    async def scenario() -> None:
        with pytest.raises(InvalidDispatchContract, match="more than once"):
            await ApplicationBus(registry, middleware=(twice,)).dispatch(
                _Increment(1),
                context=_context(),
            )
        with pytest.raises(InvalidDispatchContract, match="identity"):
            await ApplicationBus(registry, middleware=(replace_message,)).dispatch(
                _Increment(1),
                context=_context(),
            )

    asyncio.run(scenario())


def test_dispatch_context_is_ambient_during_handler_and_isolated_concurrently() -> None:
    registry = HandlerRegistry()

    async def handler(message, context):
        await asyncio.sleep(0)
        assert maybe_current_context() is context
        return _result((message.key, context.tenant_id, context.correlation_id))

    registry.register(_Read, handler)
    bus = ApplicationBus(registry)

    async def scenario() -> None:
        first, second = await asyncio.gather(
            bus.dispatch(_Read("a"), context=_context("one")),
            bus.dispatch(_Read("b"), context=_context("two")),
        )
        assert first.value == ("a", "tenant-one", "correlation-one")
        assert second.value == ("b", "tenant-two", "correlation-two")
        assert maybe_current_context() is None

    asyncio.run(scenario())


def test_precancelled_context_and_task_cancellation_never_execute_or_sanitize() -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()
    registry = HandlerRegistry()

    async def handler(_message, _context):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _result(None)

    registry.register(_Increment, handler)
    bus = ApplicationBus(registry, middleware=(SanitizedFailureMiddleware(),))

    async def scenario() -> None:
        context = _context()
        context.cancellation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bus.dispatch(_Increment(1), context=context)
        assert calls == 0

        task = asyncio.create_task(bus.dispatch(_Increment(2), context=_context("live")))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls == 1


def test_sanitized_failure_middleware_preserves_protected_cause() -> None:
    registry = HandlerRegistry()

    def handler(_message, _context):
        raise RuntimeError("private resource token=secret")

    registry.register(_Increment, handler)
    bus = ApplicationBus(registry, middleware=(SanitizedFailureMiddleware(),))

    async def scenario() -> None:
        with pytest.raises(PlatformError) as captured:
            await bus.dispatch(_Increment(1), context=_context())
        assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
        assert isinstance(captured.value.protected_cause, RuntimeError)
        assert "secret" not in str(captured.value)

    asyncio.run(scenario())


def test_gate_default_denies_before_handler_without_leaking_resource_existence() -> None:
    calls = 0
    registry = HandlerRegistry()

    def handler(_message, _context):
        nonlocal calls
        calls += 1
        return _result("private")

    registry.register(_Read, handler)
    gate = Gate(policy_version="policy-v1", policies=())
    middleware = GateMiddleware(
        gate,
        authorize=lambda message, _context: AuthorizationRequest(
            action="read",
            resource_type="task",
            protected_resource_id=message.key,
            required_scope=AcceptanceScope.DETERMINISTIC,
        ),
    )
    bus = ApplicationBus(registry, middleware=(middleware,))

    async def scenario() -> None:
        with pytest.raises(PlatformError) as captured:
            await bus.dispatch(_Read("does-not-exist-secret"), context=_context())
        assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED
        assert "does-not-exist-secret" not in str(captured.value)

    asyncio.run(scenario())
    assert calls == 0


def test_gate_denial_overrides_allow_and_authorized_result_retains_evidence() -> None:
    request = AuthorizationRequest(
        action="read",
        resource_type="task",
        protected_resource_id="private-id",
        required_scope=AcceptanceScope.DETERMINISTIC,
    )
    allow = GateDecision(
        effect=GateEffect.ALLOW,
        policy_version="policy-v1",
        reason_code="role_allowed",
        evidence=(PolicyEvidence("role", "operator"),),
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )
    deny = GateDecision(
        effect=GateEffect.DENY,
        policy_version="policy-v1",
        reason_code="tenant_denied",
        evidence=(PolicyEvidence("tenant", "mismatch"),),
    )
    gate = Gate(
        policy_version="policy-v1",
        policies=(
            lambda _request, _context: allow,
            lambda _request, _context: deny,
        ),
    )
    assert gate.decide(request, _context()).effect is GateEffect.DENY

    registry = HandlerRegistry()
    registry.register(_Read, lambda message, _context: _result(message.key))
    allowed_gate = Gate(
        policy_version="policy-v1",
        policies=(lambda _request, _context: allow,),
    )
    bus = ApplicationBus(
        registry,
        middleware=(
            GateMiddleware(
                allowed_gate,
                authorize=lambda _message, _context: request,
            ),
        ),
    )

    async def scenario() -> None:
        result = await bus.dispatch(_Read("known"), context=_context())
        assert result.value == "known"
        assert result.gate_decision == allow

    asyncio.run(scenario())
    assert "private-id" not in repr(request)
    assert "operator" not in repr(allow)


def test_gate_rejects_mismatched_policy_version_and_scope_widening() -> None:
    request = AuthorizationRequest(
        action="read",
        resource_type="task",
        protected_resource_id=None,
        required_scope=AcceptanceScope.DETERMINISTIC,
    )
    mismatched = GateDecision(
        effect=GateEffect.ALLOW,
        policy_version="other-policy",
        reason_code="allowed",
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )
    widened = GateDecision(
        effect=GateEffect.ALLOW,
        policy_version="policy-v1",
        reason_code="allowed",
        effective_scope=AcceptanceScope.REVIEWED_GENERATIVE,
    )
    for decision in (mismatched, widened):
        gate = Gate(
            policy_version="policy-v1",
            policies=(lambda _request, _context, current=decision: current,),
        )
        with pytest.raises(InvalidDispatchContract):
            gate.decide(request, _context())


def test_provenance_and_results_are_immutable_and_validate_derivation_identity() -> None:
    derivation = DerivationEvidence(
        source_id="fixture",
        source_digest=_DIGEST_A,
        relationship="derived-from",
        model_derived=False,
    )
    provenance = ProvenanceRecord(
        operation_id="task.read",
        operation_version="1.0.0",
        input_digest=_DIGEST_A,
        output_digest=_DIGEST_B,
        derivations=(derivation,),
    )
    result = ApplicationResult(value={"answer": 42}, provenance=(provenance,))

    assert result.provenance == (provenance,)
    with pytest.raises((AttributeError, TypeError)):
        result.provenance = ()  # type: ignore[misc]
    with pytest.raises(ValueError):
        DerivationEvidence("fixture", "bad", "derived-from", False)
    with pytest.raises(ValueError):
        ProvenanceRecord(
            operation_id="task.read",
            operation_version="1.0.0",
            input_digest=_DIGEST_A,
            output_digest=_DIGEST_B,
            derivations=(derivation, derivation),
        )


def test_application_layer_has_no_transport_or_provider_sdk_imports() -> None:
    import ast
    from pathlib import Path

    source = Path("src/af/application/bus.py").read_text(encoding="utf-8")
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imported.isdisjoint(
        {
            "fastapi",
            "strawberry",
            "typer",
            "mcp",
            "sqlalchemy",
            "httpx",
            "boto3",
            "redis",
        }
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"effect": "allow"},
        {"policy_version": ""},
        {"reason_code": "BAD CODE"},
        {"evidence": (object(),)},
        {
            "evidence": (
                PolicyEvidence("role", "operator"),
                PolicyEvidence("role", "operator"),
            )
        },
        {"effective_scope": "deterministic"},
        {"effect": GateEffect.DENY, "effective_scope": AcceptanceScope.CHILD},
        {"effect": GateEffect.ALLOW, "effective_scope": None},
    ],
)
def test_gate_decision_rejects_invalid_or_ambiguous_contracts(kwargs) -> None:
    values = {
        "effect": GateEffect.DENY,
        "policy_version": "policy-v1",
        "reason_code": "denied",
        "evidence": (),
        "effective_scope": None,
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        GateDecision(**values)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "BAD ACTION"},
        {"resource_type": ""},
        {"protected_resource_id": ""},
        {"protected_resource_id": "bad\nid"},
        {"required_scope": "deterministic"},
    ],
)
def test_authorization_request_rejects_invalid_public_and_protected_fields(kwargs) -> None:
    values = {
        "action": "read",
        "resource_type": "task",
        "protected_resource_id": None,
        "required_scope": AcceptanceScope.DETERMINISTIC,
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        AuthorizationRequest(**values)


def test_gate_validates_configuration_policy_results_and_protected_failures() -> None:
    request = AuthorizationRequest(
        "read",
        "task",
        None,
        AcceptanceScope.DETERMINISTIC,
    )
    context = _context()
    with pytest.raises(InvalidDispatchContract):
        Gate(policy_version="", policies=())
    with pytest.raises(InvalidDispatchContract):
        Gate(policy_version="policy-v1", policies=(object(),))

    gate = Gate(
        policy_version="policy-v1",
        policies=(lambda _request, _context: None,),
    )
    assert gate.policy_version == "policy-v1"
    assert gate.decide(request, context).reason_code == "default_deny"
    with pytest.raises(InvalidDispatchContract):
        gate.decide(object(), context)

    invalid = Gate(
        policy_version="policy-v1",
        policies=(lambda _request, _context: object(),),
    )
    with pytest.raises(InvalidDispatchContract):
        invalid.decide(request, context)

    failing = Gate(
        policy_version="policy-v1",
        policies=(
            lambda _request, _context: (_ for _ in ()).throw(
                RuntimeError("private resource exists")
            ),
        ),
    )
    with pytest.raises(PlatformError) as captured:
        failing.decide(request, context)
    assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED
    assert "private resource" not in str(captured.value)


def test_gate_cannot_widen_an_existing_effective_scope() -> None:
    request = AuthorizationRequest(
        "read",
        "task",
        None,
        AcceptanceScope.DETERMINISTIC,
    )
    decision = GateDecision(
        GateEffect.ALLOW,
        "policy-v1",
        "allowed",
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )
    context = ExecutionContext(
        principal=_context().principal,
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.CHILD,
    )
    gate = Gate(
        policy_version="policy-v1",
        policies=(lambda _request, _context: decision,),
    )
    with pytest.raises(InvalidDispatchContract, match="effective"):
        gate.decide(request, context)


def test_value_contracts_reject_invalid_flags_collections_and_gate_projection() -> None:
    derivation = DerivationEvidence("fixture", _DIGEST_A, "derived-from", False)
    provenance = ProvenanceRecord(
        "task.read",
        "1.0.0",
        _DIGEST_A,
        _DIGEST_B,
        (derivation,),
    )
    with pytest.raises(ValueError):
        DerivationEvidence("fixture", _DIGEST_A, "derived-from", 1)
    with pytest.raises(ValueError):
        ProvenanceRecord(
            "task.read",
            "1.0.0",
            _DIGEST_A,
            _DIGEST_B,
            (object(),),
        )
    with pytest.raises(ValueError):
        ApplicationResult(None, provenance=(object(),))
    with pytest.raises(ValueError):
        ApplicationResult(None, provenance=(provenance, provenance))
    with pytest.raises(ValueError):
        ApplicationResult(None, gate_decision=object())
    assert "[REDACTED]" in repr(PolicyEvidence("role", "operator"))


def test_dispatch_request_and_message_type_edge_contracts_fail_closed() -> None:
    context = _context()
    with pytest.raises(InvalidDispatchContract):
        DispatchRequest(object(), MessageKind.COMMAND, context)
    with pytest.raises(InvalidDispatchContract):
        DispatchRequest(_Increment(1), "command", context)
    with pytest.raises(InvalidDispatchContract):
        DispatchRequest(_Increment(1), MessageKind.QUERY, context)
    with pytest.raises(InvalidDispatchContract):
        DispatchRequest(_Increment(1), MessageKind.COMMAND, object())

    registry = HandlerRegistry()
    with pytest.raises(InvalidDispatchContract, match="class"):
        registry.register(object(), lambda _message, _context: _result(None))

    class PlainCommand(Command):
        pass

    with pytest.raises(InvalidDispatchContract, match="dataclass"):
        registry.register(PlainCommand, lambda _message, _context: _result(None))

    @dataclass(frozen=True, slots=True)
    class Both(Command, Query):
        value: int

    with pytest.raises(InvalidDispatchContract, match="exactly one"):
        registry.register(Both, lambda _message, _context: _result(None))

    @dataclass(frozen=True, slots=True)
    class DerivedField(Command):
        value: int
        hidden: int = field(init=False, default=1)

    with pytest.raises(InvalidDispatchContract, match="constructor"):
        registry.register(DerivedField, lambda _message, _context: _result(None))


def test_registry_and_bus_reject_invalid_runtime_shapes() -> None:
    registry = HandlerRegistry()
    assert not registry.frozen
    with pytest.raises(DispatchError):
        registry.kind_for(_Increment)
    registry.register(_Increment, lambda _message, _context: object())
    with pytest.raises(InvalidDispatchContract):
        ApplicationBus(object())
    with pytest.raises(InvalidDispatchContract):
        ApplicationBus(registry, middleware=(object(),))

    bus = ApplicationBus(registry)
    assert registry.frozen

    async def scenario() -> None:
        with pytest.raises(InvalidDispatchContract, match="context"):
            await bus.dispatch(_Increment(1), context=object())
        with pytest.raises(InvalidDispatchContract, match="invalid result"):
            await bus.dispatch(_Increment(1), context=_context())

    asyncio.run(scenario())


def test_cancellation_observed_after_middleware_prevents_handler() -> None:
    calls = 0
    registry = HandlerRegistry()

    def handler(_message, _context):
        nonlocal calls
        calls += 1
        return _result(None)

    registry.register(_Increment, handler)

    async def cancel_before_next(request, call_next):
        request.context.cancellation.cancel()
        return await call_next(request)

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await ApplicationBus(registry, middleware=(cancel_before_next,)).dispatch(
                _Increment(1),
                context=_context(),
            )

    asyncio.run(scenario())
    assert calls == 0


def test_sanitized_failure_middleware_preserves_existing_platform_error() -> None:
    expected = PlatformError(PlatformReason.VALIDATION_FAILED, stage="fixture")
    registry = HandlerRegistry()
    registry.register(
        _Increment,
        lambda _message, _context: (_ for _ in ()).throw(expected),
    )

    async def scenario() -> None:
        with pytest.raises(PlatformError) as captured:
            await ApplicationBus(
                registry,
                middleware=(SanitizedFailureMiddleware(),),
            ).dispatch(_Increment(1), context=_context())
        assert captured.value is expected

    asyncio.run(scenario())


def test_gate_middleware_handles_optional_invalid_conflicting_and_failing_resolution() -> None:
    allow = GateDecision(
        GateEffect.ALLOW,
        "policy-v1",
        "allowed",
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )
    other_allow = GateDecision(
        GateEffect.ALLOW,
        "policy-v1",
        "other_allow",
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )
    gate = Gate(
        policy_version="policy-v1",
        policies=(lambda _request, _context: allow,),
    )
    with pytest.raises(InvalidDispatchContract):
        GateMiddleware(object(), authorize=lambda _message, _context: None)
    with pytest.raises(InvalidDispatchContract):
        GateMiddleware(gate, authorize=object())

    registry = HandlerRegistry()
    registry.register(_Read, lambda message, _context: _result(message.key))
    optional = ApplicationBus(
        registry,
        middleware=(GateMiddleware(gate, authorize=lambda _message, _context: None),),
    )

    async def optional_scenario() -> None:
        assert (await optional.dispatch(_Read("ok"), context=_context())).value == "ok"

    asyncio.run(optional_scenario())

    request = AuthorizationRequest(
        "read",
        "task",
        None,
        AcceptanceScope.DETERMINISTIC,
    )

    async def failure_scenarios() -> None:
        invalid_registry = HandlerRegistry()
        invalid_registry.register(_Read, lambda _message, _context: _result(None))
        invalid = ApplicationBus(
            invalid_registry,
            middleware=(GateMiddleware(gate, authorize=lambda _message, _context: object()),),
        )
        with pytest.raises(InvalidDispatchContract):
            await invalid.dispatch(_Read("x"), context=_context())

        failing_registry = HandlerRegistry()
        failing_registry.register(_Read, lambda _message, _context: _result(None))
        failing = ApplicationBus(
            failing_registry,
            middleware=(
                GateMiddleware(
                    gate,
                    authorize=lambda _message, _context: (_ for _ in ()).throw(
                        RuntimeError("private")
                    ),
                ),
            ),
        )
        with pytest.raises(PlatformError) as captured:
            await failing.dispatch(_Read("x"), context=_context())
        assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED

        conflict_registry = HandlerRegistry()
        conflict_registry.register(
            _Read,
            lambda _message, _context: ApplicationResult(
                None,
                gate_decision=other_allow,
            ),
        )
        conflict = ApplicationBus(
            conflict_registry,
            middleware=(GateMiddleware(gate, authorize=lambda _message, _context: request),),
        )
        with pytest.raises(InvalidDispatchContract, match="conflicts"):
            await conflict.dispatch(_Read("x"), context=_context())

    asyncio.run(failure_scenarios())
