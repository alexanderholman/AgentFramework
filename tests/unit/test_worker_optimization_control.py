from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from af.application.bus import Gate, GateDecision, GateEffect
from af.application.worker_generation import WorkerRuntimeOptimization
from af.application.worker_optimization_control import (
    WorkerOptimizationControlService,
    WorkerOptimizationPromotion,
)
from af.application.worker_qualification import (
    WorkerOptimizationCandidate,
    WorkerOptimizationPriorities,
    WorkerOptimizationQualification,
    WorkerOptimizationSelection,
)
from af.framework.context import AcceptanceScope, ExecutionContext, Principal
from af.framework.errors import InvalidProviderConfiguration, PlatformError, PlatformReason
from af.framework.managers import CapabilityBinding

NOW = datetime(2026, 8, 18, 12, tzinfo=UTC)


def _context() -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(
            subject_id="operator-1",
            principal_type="user",
            roles=("operator",),
        ),
        requested_scope=AcceptanceScope.OPERATOR_EVALUATION,
    )


def _candidate(
    evidence: str,
    *,
    environment: str = "e" * 64,
    performance: int = 10,
    safety: int = 20,
    cost: int = 30,
) -> WorkerOptimizationCandidate:
    optimization = WorkerRuntimeOptimization(
        worker_count=1,
        max_concurrency=2,
        poll_interval=timedelta(milliseconds=10),
        heartbeat_interval=timedelta(seconds=1),
        max_idle_time=None,
        queue_wait_alert_after=None,
        renewal_min_interval=timedelta(milliseconds=100),
        renewal_max_interval=timedelta(seconds=1),
    )
    qualification = WorkerOptimizationQualification(
        driver_name="database",
        binding=CapabilityBinding(
            driver_version="1.0.0",
            artifact_digest="a" * 64,
            dependency_graph_digest="b" * 64,
            configuration_digest="c" * 64,
            deployment_profile="test",
            policy_version="policy-v1",
        ),
        optimization=optimization,
        evidence_digest=evidence,
        environment_digest=environment,
        observed_at=NOW - timedelta(hours=1),
        expires_at=NOW + timedelta(hours=1),
        sample_count=100,
        real_service=True,
        correctness_failures=0,
        settlement_failures=0,
        reservation_losses=0,
    )
    return WorkerOptimizationCandidate(qualification, performance, safety, cost)


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[
            tuple[WorkerOptimizationSelection, WorkerOptimizationPriorities, datetime]
        ] = []

    def record(
        self,
        selection: WorkerOptimizationSelection,
        *,
        priorities: WorkerOptimizationPriorities,
        now: datetime,
    ) -> None:
        self.calls.append((selection, priorities, now))


def _allowing_gate(requests: list[tuple[str, str, str | None]]) -> Gate:
    def allow(request, context):  # type: ignore[no-untyped-def]
        del context
        requests.append((request.action, request.resource_type, request.protected_resource_id))
        return GateDecision(
            effect=GateEffect.ALLOW,
            policy_version="policy-v1",
            reason_code="operator_allowed",
            effective_scope=AcceptanceScope.OPERATOR_EVALUATION,
        )

    return Gate(policy_version="policy-v1", policies=(allow,))


def test_worker_optimization_control_denies_before_selection_or_recording() -> None:
    class ExplodingCandidates:
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("candidates must not be inspected before authorization")

    recorder = _Recorder()
    service = WorkerOptimizationControlService(
        recorder,
        gate=Gate(policy_version="policy-v1", policies=()),
        clock=lambda: NOW,
    )

    with pytest.raises(PlatformError) as denied:
        service.select_and_record(
            ExplodingCandidates(),
            priorities=WorkerOptimizationPriorities(40, 40, 20),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )

    assert denied.value.reason is PlatformReason.AUTHORIZATION_DENIED
    assert recorder.calls == []


def test_worker_optimization_control_records_exact_authorized_selection() -> None:
    requests: list[tuple[str, str, str | None]] = []
    recorder = _Recorder()
    priorities = WorkerOptimizationPriorities(100, 0, 0)
    candidates = (
        _candidate("d" * 64, performance=10),
        _candidate("f" * 64, performance=20),
    )
    service = WorkerOptimizationControlService(
        recorder,
        gate=_allowing_gate(requests),
        clock=lambda: NOW,
    )

    promotion = service.select_and_record(
        candidates,
        priorities=priorities,
        context=_context(),
        driver_name="database",
        environment_digest="e" * 64,
    )

    assert isinstance(promotion, WorkerOptimizationPromotion)
    assert promotion.selection.qualification.evidence_digest == "f" * 64
    assert promotion.authorization.effect is GateEffect.ALLOW
    assert recorder.calls == [(promotion.selection, priorities, NOW)]
    assert requests == [
        ("promote", "worker-optimization", f"database:{'e' * 64}"),
    ]
    assert "operator-1" not in repr(promotion)


@pytest.mark.parametrize(
    "driver_name, environment_digest",
    [
        ("other", "e" * 64),
        ("database", "0" * 64),
    ],
)
def test_worker_optimization_control_rejects_declared_target_drift_before_recording(
    driver_name: str,
    environment_digest: str,
) -> None:
    recorder = _Recorder()
    service = WorkerOptimizationControlService(
        recorder,
        gate=_allowing_gate([]),
        clock=lambda: NOW,
    )

    with pytest.raises(InvalidProviderConfiguration):
        service.select_and_record(
            (_candidate("d" * 64),),
            priorities=WorkerOptimizationPriorities(40, 40, 20),
            context=_context(),
            driver_name=driver_name,
            environment_digest=environment_digest,
        )

    assert recorder.calls == []


def test_worker_optimization_control_propagates_recording_failure() -> None:
    failure = RuntimeError("promotion failed")

    class FailingRecorder:
        def record(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise failure

    service = WorkerOptimizationControlService(
        FailingRecorder(),
        gate=_allowing_gate([]),
        clock=lambda: NOW,
    )

    with pytest.raises(RuntimeError) as raised:
        service.select_and_record(
            (_candidate("d" * 64),),
            priorities=WorkerOptimizationPriorities(40, 40, 20),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )

    assert raised.value is failure


def test_worker_optimization_promotion_rejects_invalid_boundaries() -> None:
    selection = WorkerOptimizationSelection(
        qualification=_candidate("d" * 64).qualification,
        weighted_score=1,
        candidate_count=1,
        selection_digest="f" * 64,
    )
    allow = GateDecision(
        effect=GateEffect.ALLOW,
        policy_version="policy-v1",
        reason_code="operator_allowed",
        effective_scope=AcceptanceScope.OPERATOR_EVALUATION,
    )
    deny = GateDecision(
        effect=GateEffect.DENY,
        policy_version="policy-v1",
        reason_code="denied",
    )
    with pytest.raises(TypeError):
        WorkerOptimizationPromotion(object(), allow)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        WorkerOptimizationPromotion(selection, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkerOptimizationPromotion(selection, deny)


@pytest.mark.parametrize(
    "recorder, gate, selector, clock",
    [
        (object(), Gate(policy_version="policy-v1", policies=()), None, lambda: NOW),
        (_Recorder(), object(), None, lambda: NOW),
        (_Recorder(), Gate(policy_version="policy-v1", policies=()), object(), lambda: NOW),
        (_Recorder(), Gate(policy_version="policy-v1", policies=()), None, object()),
    ],
)
def test_worker_optimization_control_rejects_invalid_composition(
    recorder: object,
    gate: object,
    selector: object,
    clock: object,
) -> None:
    with pytest.raises(TypeError):
        WorkerOptimizationControlService(
            recorder,  # type: ignore[arg-type]
            gate=gate,  # type: ignore[arg-type]
            selector=selector,  # type: ignore[arg-type]
            clock=clock,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "driver_name, environment_digest",
    [
        ("INVALID", "e" * 64),
        ("database", "invalid"),
    ],
)
def test_worker_optimization_control_rejects_invalid_declared_target(
    driver_name: str,
    environment_digest: str,
) -> None:
    service = WorkerOptimizationControlService(
        _Recorder(),
        gate=_allowing_gate([]),
        clock=lambda: NOW,
    )
    with pytest.raises(InvalidProviderConfiguration):
        service.select_and_record(
            (_candidate("d" * 64),),
            priorities=WorkerOptimizationPriorities(40, 40, 20),
            context=_context(),
            driver_name=driver_name,
            environment_digest=environment_digest,
        )


def test_worker_optimization_control_rejects_naive_clock_before_selection() -> None:
    service = WorkerOptimizationControlService(
        _Recorder(),
        gate=_allowing_gate([]),
        clock=lambda: datetime(2026, 8, 18),
    )
    with pytest.raises(InvalidProviderConfiguration):
        service.select_and_record(
            (_candidate("d" * 64),),
            priorities=WorkerOptimizationPriorities(40, 40, 20),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )


@pytest.mark.parametrize("kind", ["coroutine", "async_generator", "awaitable", "value"])
def test_worker_optimization_control_rejects_invalid_recorder_results(kind: str) -> None:
    async def coroutine_result() -> None:
        return None

    async def generator_result():  # type: ignore[no-untyped-def]
        yield None

    class AwaitableResult:
        def __await__(self):  # type: ignore[no-untyped-def]
            yield

    class InvalidRecorder:
        def record(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            if kind == "coroutine":
                return coroutine_result()
            if kind == "async_generator":
                return generator_result()
            if kind == "awaitable":
                return AwaitableResult()
            return "unexpected"

    service = WorkerOptimizationControlService(
        InvalidRecorder(),
        gate=_allowing_gate([]),
        clock=lambda: NOW,
    )
    with pytest.raises(TypeError):
        service.select_and_record(
            (_candidate("d" * 64),),
            priorities=WorkerOptimizationPriorities(40, 40, 20),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )
