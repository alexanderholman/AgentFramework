from __future__ import annotations

import json
import runpy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from af.application.resource_leases import ResourceLeaseService
from af.application.worker_generation import (
    PinnedWorkerOptimizationProvider,
    WorkerGenerationFactory,
    WorkerOptimizationContext,
    WorkerRuntimeOptimization,
)
from af.application.worker_qualification import (
    WorkerOptimizationCandidate,
    WorkerOptimizationPriorities,
    WorkerOptimizationQualification,
    WorkerOptimizationSelection,
    WorkerOptimizationSelector,
)
from af.application.worker_resource_admission import (
    FencedResourceLeaseAdmissionGate,
    FencedResourceLeasePermit,
)
from af.application.worker_supervision import WorkerSupervisorFactory
from af.domain.resources import (
    FenceToken,
    LeaseState,
    ResourceBudget,
    ResourceIdentity,
    ResourceKind,
)
from af.framework.errors import InvalidProviderConfiguration
from af.framework.managers import (
    CapabilityBinding,
    ConnectionRef,
)
from af.infrastructure.resource_leases import InMemoryLeaseRepository
from af.infrastructure.worker_optimization import (
    load_worker_optimization_profile,
    record_worker_optimization_profile,
    worker_queue_storage_identity,
)
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import contracts as queue_contracts
from af.queue import worker as worker_module
from af.queue.contracts import (
    QueueDeadLetter,
    QueueDeadLetterReplayReceipt,
    QueueJob,
    QueueJobTags,
    QueueReservation,
)
from af.queue.worker import InMemoryIdempotencyStore, WorkerKernel

NOW = datetime(2026, 8, 18, 10, tzinfo=UTC)


def _binding(**changes: object) -> CapabilityBinding:
    values: dict[str, object] = {
        "driver_version": "1.0.0",
        "artifact_digest": "a" * 64,
        "dependency_graph_digest": "b" * 64,
        "configuration_digest": "c" * 64,
        "deployment_profile": "test",
        "policy_version": "policy-v1",
    }
    values.update(changes)
    return CapabilityBinding(**values)  # type: ignore[arg-type]


def _optimization(**changes: object) -> WorkerRuntimeOptimization:
    values: dict[str, object] = {
        "worker_count": 1,
        "max_concurrency": 2,
        "poll_interval": timedelta(milliseconds=10),
        "heartbeat_interval": timedelta(seconds=1),
        "max_idle_time": None,
        "queue_wait_alert_after": None,
        "renewal_min_interval": timedelta(milliseconds=100),
        "renewal_max_interval": timedelta(seconds=1),
    }
    values.update(changes)
    return WorkerRuntimeOptimization(**values)  # type: ignore[arg-type]


def _qualification(**changes: object) -> WorkerOptimizationQualification:
    values: dict[str, object] = {
        "driver_name": "database",
        "binding": _binding(),
        "optimization": _optimization(),
        "evidence_digest": "d" * 64,
        "environment_digest": "e" * 64,
        "observed_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(hours=1),
        "sample_count": 100,
        "real_service": True,
        "correctness_failures": 0,
        "settlement_failures": 0,
        "reservation_losses": 0,
    }
    values.update(changes)
    return WorkerOptimizationQualification(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"worker_count": 0},
        {"worker_count": True},
        {"max_concurrency": 0},
        {"worker_count": 3, "max_concurrency": 2},
        {"poll_interval": timedelta(0)},
        {"heartbeat_interval": timedelta(0)},
        {"max_idle_time": timedelta(0)},
        {"queue_wait_alert_after": timedelta(0)},
        {"renewal_min_interval": timedelta(0)},
        {
            "renewal_min_interval": timedelta(seconds=2),
            "renewal_max_interval": timedelta(seconds=1),
        },
    ],
)
def test_runtime_optimization_rejects_every_invalid_boundary(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _optimization(**changes)


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"driver_name": "INVALID"}, ValueError),
        ({"binding": object()}, TypeError),
        ({"optimization": object()}, TypeError),
        ({"evidence_digest": "A" * 64}, ValueError),
        ({"environment_digest": "short"}, ValueError),
        ({"observed_at": datetime(2026, 1, 1)}, ValueError),
        ({"expires_at": datetime(2026, 1, 1)}, ValueError),
        ({"expires_at": NOW - timedelta(hours=2)}, ValueError),
        ({"sample_count": 0}, ValueError),
        ({"real_service": 1}, ValueError),
        ({"correctness_failures": -1}, ValueError),
        ({"settlement_failures": -1}, ValueError),
        ({"reservation_losses": -1}, ValueError),
    ],
)
def test_qualification_rejects_every_invalid_boundary(
    changes: dict[str, object], error: type[Exception]
) -> None:
    with pytest.raises(error):
        _qualification(**changes)


def test_qualification_normalizes_non_utc_times() -> None:
    offset = UTC
    qualification = _qualification(
        observed_at=NOW.astimezone(offset), expires_at=(NOW + timedelta(1)).astimezone(offset)
    )
    assert qualification.observed_at.tzinfo is UTC
    assert qualification.expires_at.tzinfo is UTC


@pytest.mark.parametrize(
    "factory",
    [
        lambda: WorkerOptimizationPriorities(-1, 1, 100),
        lambda: WorkerOptimizationPriorities(1, True, 98),
        lambda: WorkerOptimizationPriorities(50, 25, 24),
        lambda: WorkerOptimizationCandidate(object(), 1, 1, 1),
        lambda: WorkerOptimizationCandidate(_qualification(), -1, 1, 1),
        lambda: WorkerOptimizationCandidate(_qualification(), 1, 1_000_001, 1),
        lambda: WorkerOptimizationCandidate(_qualification(), 1, 1, False),
        lambda: WorkerOptimizationSelection(object(), 1, 1, "f" * 64),
        lambda: WorkerOptimizationSelection(_qualification(), -1, 1, "f" * 64),
        lambda: WorkerOptimizationSelection(_qualification(), 1, 0, "f" * 64),
        lambda: WorkerOptimizationSelection(_qualification(), 1, 257, "f" * 64),
        lambda: WorkerOptimizationSelection(_qualification(), 1, 1, "invalid"),
    ],
)
def test_selection_value_objects_reject_invalid_boundaries(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def _candidate(**qualification_changes: object) -> WorkerOptimizationCandidate:
    return WorkerOptimizationCandidate(_qualification(**qualification_changes), 10, 20, 30)


@pytest.mark.parametrize(
    "candidates, now",
    [
        ([], NOW),
        ([object()], NOW),
        ([_candidate(observed_at=NOW + timedelta(seconds=1))], NOW),
        ([_candidate(expires_at=NOW)], NOW),
        ([_candidate(real_service=False)], NOW),
        ([_candidate(correctness_failures=1)], NOW),
        ([_candidate(settlement_failures=1)], NOW),
        ([_candidate(reservation_losses=1)], NOW),
        ([_candidate(), _candidate(evidence_digest="d" * 64)], NOW),
        ([_candidate(), _candidate(driver_name="other", evidence_digest="f" * 64)], NOW),
    ],
)
def test_selector_rejects_unsafe_or_ambiguous_candidate_sets(
    candidates: object, now: datetime
) -> None:
    with pytest.raises((TypeError, InvalidProviderConfiguration)):
        WorkerOptimizationSelector().select(
            candidates,  # type: ignore[arg-type]
            priorities=WorkerOptimizationPriorities(40, 40, 20),
            now=now,
        )


def test_selector_rejects_invalid_priority_time_and_non_iterable() -> None:
    selector = WorkerOptimizationSelector()
    with pytest.raises(TypeError):
        selector.select([_candidate()], priorities=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        selector.select(1, priorities=WorkerOptimizationPriorities(40, 40, 20))  # type: ignore[arg-type]
    with pytest.raises(InvalidProviderConfiguration):
        selector.select(
            [_candidate()],
            priorities=WorkerOptimizationPriorities(40, 40, 20),
            now=datetime(2026, 1, 1),
        )


def test_selector_score_and_digest_cover_optional_duration_payload() -> None:
    first = _candidate()
    second = WorkerOptimizationCandidate(
        _qualification(
            evidence_digest="f" * 64,
            optimization=_optimization(
                max_idle_time=timedelta(1), queue_wait_alert_after=timedelta(seconds=2)
            ),
        ),
        20,
        20,
        10,
    )
    priorities = WorkerOptimizationPriorities(50, 30, 20)
    assert first.weighted_score(priorities) == 1700
    with pytest.raises(TypeError):
        first.weighted_score(object())  # type: ignore[arg-type]
    selection = WorkerOptimizationSelector().select([first, second], priorities=priorities, now=NOW)
    assert selection.candidate_count == 2
    assert len(selection.selection_digest) == 64


def _profile_document() -> dict[str, object]:
    qualification = _qualification()
    optimization = qualification.optimization
    return {
        "schema_version": 2,
        "driver_name": qualification.driver_name,
        "binding": asdict(qualification.binding),
        "qualification": {
            "evidence_digest": qualification.evidence_digest,
            "environment_digest": qualification.environment_digest,
            "observed_at": "2026-08-18T09:00:00.000000Z",
            "expires_at": "2026-08-18T11:00:00.000000Z",
            "sample_count": qualification.sample_count,
            "real_service": True,
            "correctness_failures": 0,
            "settlement_failures": 0,
            "reservation_losses": 0,
        },
        "optimization": {
            "worker_count": optimization.worker_count,
            "max_concurrency": optimization.max_concurrency,
            "poll_interval_ms": 10,
            "heartbeat_interval_ms": 1000,
            "max_idle_time_ms": None,
            "queue_wait_alert_after_ms": None,
            "renewal_min_interval_ms": 100,
            "renewal_max_interval_ms": 1000,
        },
    }


@pytest.mark.parametrize(
    "section, field, value",
    [
        ("root", "schema_version", True),
        ("root", "driver_name", 1),
        ("binding", "driver_version", 1),
        ("optimization", "worker_count", 0),
        ("optimization", "max_concurrency", True),
        ("optimization", "poll_interval_ms", -1),
        ("optimization", "max_idle_time_ms", 0),
        ("qualification", "observed_at", "2026-08-18"),
        ("qualification", "sample_count", 0),
        ("qualification", "real_service", 1),
        ("qualification", "correctness_failures", -1),
        ("qualification", "settlement_failures", -1),
        ("qualification", "reservation_losses", -1),
    ],
)
def test_profile_loader_rejects_invalid_field_types(
    tmp_path: Path, section: str, field: str, value: object
) -> None:
    document = _profile_document()
    target = document if section == "root" else document[section]
    assert isinstance(target, dict)
    target[field] = value
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(InvalidProviderConfiguration):
        load_worker_optimization_profile(path, now=NOW)


def test_profile_io_rejects_invalid_targets_and_identity(tmp_path: Path) -> None:
    with pytest.raises(InvalidProviderConfiguration):
        record_worker_optimization_profile(
            tmp_path / "missing" / "p.json", _qualification(), now=NOW
        )
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(InvalidProviderConfiguration):
        record_worker_optimization_profile(directory, _qualification(), now=NOW)
    with pytest.raises(InvalidProviderConfiguration):
        record_worker_optimization_profile(tmp_path / "x", object(), now=NOW)  # type: ignore[arg-type]
    empty = tmp_path / "empty.json"
    empty.write_bytes(b"")
    with pytest.raises(InvalidProviderConfiguration):
        load_worker_optimization_profile(empty, now=NOW)
    assert worker_queue_storage_identity(tmp_path) == worker_queue_storage_identity(str(tmp_path))
    with pytest.raises(InvalidProviderConfiguration):
        worker_queue_storage_identity(None)  # type: ignore[arg-type]


def test_generation_and_pinned_provider_reject_invalid_boundaries() -> None:
    async def async_factory() -> object:
        return object()

    with pytest.raises(TypeError):
        WorkerGenerationFactory(async_factory, optimizers={})
    with pytest.raises(TypeError):
        WorkerGenerationFactory(lambda: object(), optimizers=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkerGenerationFactory(
            lambda: object(), optimizers={"INVALID": lambda context: _optimization()}
        )

    binding = _binding()
    values = _optimization()
    for kwargs, error in [
        ({"driver_name": "INVALID"}, ValueError),
        ({"binding": object()}, TypeError),
        ({"profile_digest": "x"}, ValueError),
        ({"qualification_digest": "x"}, ValueError),
        ({"optimization": object()}, TypeError),
    ]:
        base: dict[str, object] = {
            "driver_name": "database",
            "binding": binding,
            "profile_digest": "a" * 64,
            "qualification_digest": "b" * 64,
            "optimization": values,
        }
        base.update(kwargs)
        with pytest.raises(error):
            PinnedWorkerOptimizationProvider(**base)  # type: ignore[arg-type]

    provider = PinnedWorkerOptimizationProvider(
        driver_name="database",
        binding=binding,
        profile_digest="a" * 64,
        qualification_digest="b" * 64,
        optimization=values,
    )
    assert provider.driver_name == "database"
    assert provider.profile_digest == "a" * 64
    assert provider.qualification_digest == "b" * 64
    assert provider.binding == binding
    with pytest.raises(TypeError):
        provider(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkerOptimizationContext(ConnectionRef("cache", "primary", "database"), Mock())
    with pytest.raises(ValueError):
        WorkerOptimizationContext(ConnectionRef("queue", "primary", "database"), object())


class _Driver:
    def reserve(self, *, lane: str | None = None) -> None:
        return None

    def ack(self, reservation: object) -> None:
        pass

    def reject(self, reservation: object) -> None:
        pass

    def release(self, reservation: object) -> None:
        pass

    def retry(self, reservation: object, *, available_at: object = None) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrency": 0},
        {"heartbeat_interval": timedelta(0)},
        {"max_idle_time": timedelta(0)},
        {"idempotency": object()},
        {"scope_factory": object()},
        {"admission_gate": object()},
        {"connection_recycler": object()},
        {"driver_close": object()},
        {"telemetry": object()},
        {"queue_wait_alert_after": timedelta(0)},
        {"renewal_min_interval": timedelta(0)},
        {
            "renewal_min_interval": timedelta(seconds=2),
            "renewal_max_interval": timedelta(seconds=1),
        },
        {"clock": object()},
    ],
)
def test_worker_kernel_rejects_invalid_boundaries(kwargs: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        WorkerKernel(_Driver(), **kwargs)  # type: ignore[arg-type]


def test_worker_helpers_cover_all_operational_buckets() -> None:
    assert [
        worker_module._duration_bucket(value)
        for value in (
            timedelta(0),
            timedelta(milliseconds=50),
            timedelta(milliseconds=500),
            timedelta(seconds=5),
            timedelta(seconds=30),
            timedelta(minutes=2),
            timedelta(minutes=6),
        )
    ] == ["lt_10ms", "lt_100ms", "lt_1s", "lt_10s", "lt_1m", "lt_5m", "gte_5m"]
    assert [worker_module._renewal_count_bucket(value) for value in (0, 1, 2)] == [
        "none",
        "one",
        "multiple",
    ]
    assert [worker_module._saturation_bucket(value) for value in (0.1, 0.6, 0.9, 1.0)] == [
        "lt_50pct",
        "lt_80pct",
        "lt_100pct",
        "full",
    ]
    with pytest.raises(ValueError):
        InMemoryIdempotencyStore(capacity=0)


def _budget(*, duration: timedelta | None = timedelta(minutes=2)) -> ResourceBudget:
    return ResourceBudget(
        identity=ResourceIdentity(ResourceKind.CONCURRENCY, "host", "workers"),
        max_quantity=2,
        max_duration=duration,
        max_concurrent=2,
    )


def _service() -> ResourceLeaseService:
    return ResourceLeaseService(InMemoryLeaseRepository(), clock=lambda: NOW)


def _active_lease(service: ResourceLeaseService):  # type: ignore[no-untyped-def]
    created = service.create_lease("lease", _budget(), 1, {}, "operator", "auth")
    assert created.fence_token is not None
    return service.activate_lease(created.id, created.fence_token, "auth")


@pytest.mark.parametrize(
    "service, lease, authorization, interval, thread_factory, error",
    [
        (object(), object(), "auth", None, Mock(), TypeError),
        (_service(), object(), "auth", None, Mock(), ValueError),
        (_service(), object(), "", None, Mock(), ValueError),
        (_service(), object(), "auth", timedelta(0), Mock(), ValueError),
        (_service(), object(), "auth", None, object(), TypeError),
    ],
)
def test_resource_permit_rejects_invalid_boundaries(
    service: object,
    lease: object,
    authorization: str,
    interval: timedelta | None,
    thread_factory: object,
    error: type[Exception],
) -> None:
    if isinstance(service, ResourceLeaseService) and not hasattr(lease, "state"):
        lease = (
            _active_lease(service)
            if authorization == "" or interval == timedelta(0) or not callable(thread_factory)
            else lease
        )
    with pytest.raises(error):
        FencedResourceLeasePermit(
            service,  # type: ignore[arg-type]
            lease,  # type: ignore[arg-type]
            authorization_digest=authorization,
            renewal_interval=interval,
            thread_factory=thread_factory,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "changes, error",
    [
        ({"service": object()}, TypeError),
        ({"budget": object()}, TypeError),
        ({"quantity": 0}, ValueError),
        ({"quantity": True}, ValueError),
        ({"quantity": float("nan")}, ValueError),
        ({"quantity": float("inf")}, ValueError),
        ({"quantity": float("-inf")}, ValueError),
        ({"authorized_by": ""}, ValueError),
        ({"authorization_digest": ""}, ValueError),
        ({"lease_id_factory": object()}, TypeError),
        ({"thread_factory": object()}, TypeError),
        ({"scope": {1: "bad"}}, ValueError),
        ({"renewal_interval": timedelta(0)}, ValueError),
        ({"renewal_interval": timedelta(minutes=2)}, ValueError),
        ({"budget": _budget(duration=None), "renewal_interval": timedelta(seconds=1)}, ValueError),
    ],
)
def test_resource_gate_rejects_invalid_boundaries(
    changes: dict[str, object], error: type[Exception]
) -> None:
    values: dict[str, object] = {
        "service": _service(),
        "budget": _budget(),
        "quantity": 1,
        "scope": {},
        "authorized_by": "operator",
        "authorization_digest": "auth",
        "lease_id_factory": lambda: "lease",
        "renewal_interval": timedelta(seconds=30),
    }
    values.update(changes)
    with pytest.raises(error):
        FencedResourceLeaseAdmissionGate(**values)  # type: ignore[arg-type]


def test_resource_gate_capacity_lane_and_release_lifecycle() -> None:
    service = _service()
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"queue_lane": "critical"},
        authorized_by="operator",
        authorization_digest="auth",
        lease_id_factory=lambda: "lease",
        renewal_interval=None,
    )
    assert gate(lane="critical", active_jobs=2, max_concurrency=2) is False
    assert gate(lane="other", active_jobs=0, max_concurrency=2) is False
    permit = gate(lane="critical", active_jobs=0, max_concurrency=2)
    assert isinstance(permit, FencedResourceLeasePermit)
    assert permit.lease.state is LeaseState.ACTIVE
    permit.release()
    permit.release()
    assert permit.lease.state is LeaseState.RECONCILED
    with pytest.raises(RuntimeError):
        permit.renew()


def test_resource_gate_cleans_up_failed_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={},
        authorized_by="operator",
        authorization_digest="auth",
        lease_id_factory=lambda: "lease",
        renewal_interval=None,
    )
    monkeypatch.setattr(service, "activate_lease", Mock(side_effect=RuntimeError("activation")))
    with pytest.raises(RuntimeError, match="activation"):
        gate(lane=None, active_jobs=0, max_concurrency=2)
    assert service.get_lease("lease").state is LeaseState.RECONCILED


def test_resource_gate_groups_activation_and_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={},
        authorized_by="operator",
        authorization_digest="auth",
        lease_id_factory=lambda: "lease",
        renewal_interval=None,
    )
    monkeypatch.setattr(service, "activate_lease", Mock(side_effect=RuntimeError("activation")))
    monkeypatch.setattr(service, "expire_lease", Mock(side_effect=RuntimeError("cleanup")))
    with pytest.raises(BaseExceptionGroup) as raised:
        gate(lane=None, active_jobs=0, max_concurrency=2)
    assert len(raised.value.exceptions) == 2


def test_resource_permit_surfaces_renewal_and_release_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    permit = FencedResourceLeasePermit(
        service,
        _active_lease(service),
        authorization_digest="auth",
        renewal_interval=None,
    )
    renewal_failure = RuntimeError("renewal")
    permit._failure = renewal_failure
    with pytest.raises(RuntimeError, match="renewal"):
        permit.renew()
    monkeypatch.setattr(service, "release_lease", Mock(side_effect=RuntimeError("release")))
    with pytest.raises(BaseExceptionGroup) as raised:
        permit.release()
    assert raised.value.exceptions == (renewal_failure, service.release_lease.side_effect)


def test_resource_permit_surfaces_release_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    permit = FencedResourceLeasePermit(
        service,
        _active_lease(service),
        authorization_digest="auth",
        renewal_interval=None,
    )
    monkeypatch.setattr(service, "release_lease", Mock(side_effect=RuntimeError("release")))
    with pytest.raises(RuntimeError, match="release"):
        permit.release()


def test_resource_permit_retries_release_after_transient_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    permit = FencedResourceLeasePermit(
        service,
        _active_lease(service),
        authorization_digest="auth",
        renewal_interval=None,
    )
    release_lease = service.release_lease
    attempts = 0

    def transient_release(lease_id: str, fence_token: FenceToken):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient release failure")
        return release_lease(lease_id, fence_token)

    monkeypatch.setattr(service, "release_lease", transient_release)

    with pytest.raises(RuntimeError, match="transient release failure"):
        permit.release()
    assert permit.lease.state is LeaseState.ACTIVE

    permit.release()
    permit.release()

    assert attempts == 2
    assert permit.lease.state is LeaseState.RECONCILED


def test_resource_permit_surfaces_stored_renewal_failure_after_release() -> None:
    service = _service()
    permit = FencedResourceLeasePermit(
        service,
        _active_lease(service),
        authorization_digest="auth",
        renewal_interval=None,
    )
    permit._failure = RuntimeError("renewal")
    with pytest.raises(RuntimeError, match="renewal"):
        permit.release()
    assert permit.lease.state is LeaseState.RECONCILED


def test_resource_gate_groups_permit_startup_and_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()

    def fail_thread(**kwargs: object) -> object:
        raise RuntimeError("startup")

    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={},
        authorized_by="operator",
        authorization_digest="auth",
        lease_id_factory=lambda: "lease",
        renewal_interval=timedelta(seconds=30),
        thread_factory=fail_thread,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(service, "release_lease", Mock(side_effect=RuntimeError("cleanup")))
    with pytest.raises(BaseExceptionGroup) as raised:
        gate(lane=None, active_jobs=0, max_concurrency=2)
    assert len(raised.value.exceptions) == 2


@pytest.mark.parametrize("host_id", ["", "bad\nvalue", "bad\rvalue"])
def test_supervisor_factory_rejects_invalid_host(host_id: str) -> None:
    with pytest.raises(ValueError):
        WorkerSupervisorFactory(_service(), host_id=host_id, maintenance_interval=timedelta(1))


def test_supervisor_factory_rejects_invalid_service_and_interval() -> None:
    with pytest.raises(TypeError):
        WorkerSupervisorFactory(object(), host_id=None, maintenance_interval=timedelta(1))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        WorkerSupervisorFactory(_service(), host_id=None, maintenance_interval=timedelta(0))


def test_worker_kernel_async_and_driver_boundaries() -> None:
    async def asynchronous() -> None:
        return None

    class AsyncRecycler:
        async def recycle(self) -> None:
            return None

    with pytest.raises(TypeError):
        WorkerKernel(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        WorkerKernel(_Driver(), connection_recycler=AsyncRecycler())
    with pytest.raises(TypeError):
        WorkerKernel(_Driver(), driver_close=asynchronous)
    kernel = WorkerKernel(_Driver())
    with pytest.raises(ValueError):
        kernel.close(timeout=timedelta(0))
    with pytest.raises(ValueError):
        kernel.run_loop(lambda job: None, poll_interval=timedelta(0))


def test_worker_handler_validation_rejects_all_async_shapes() -> None:
    async def coroutine_handler(job: object) -> None:
        del job

    async def generator_handler(job: object):  # type: ignore[no-untyped-def]
        yield job

    class CoroutineCallable:
        async def __call__(self, job: object) -> None:
            del job

    class CooperativeWithoutInvoke:
        cooperative_cancellation = True

        def __call__(self, job: object) -> None:
            del job

    class CooperativeAsyncInvoke:
        cooperative_cancellation = True

        def __call__(self, job: object) -> None:
            del job

        async def invoke(self, job: object, *, cancellation: object) -> None:
            del job, cancellation

    for handler in (object(), coroutine_handler, generator_handler, CoroutineCallable()):
        with pytest.raises(TypeError):
            worker_module._validate_handler(handler)
    with pytest.raises(TypeError):
        worker_module._resolve_cooperative_handler(CooperativeWithoutInvoke())
    with pytest.raises(TypeError):
        worker_module._resolve_cooperative_handler(CooperativeAsyncInvoke())


def test_worker_sync_result_validation_rejects_all_async_shapes() -> None:
    async def coroutine_result() -> None:
        return None

    async def generator_result():  # type: ignore[no-untyped-def]
        yield None

    class AwaitableResult:
        def __await__(self):  # type: ignore[no-untyped-def]
            yield

    with pytest.raises(TypeError):
        worker_module._validate_sync_result(coroutine_result())
    with pytest.raises(TypeError):
        worker_module._validate_sync_result(generator_result())
    with pytest.raises(TypeError):
        worker_module._validate_sync_result(AwaitableResult())
    with pytest.raises(TypeError):
        worker_module._validate_sync_result("unexpected")


@pytest.mark.parametrize(
    "result_kind", ["coroutine", "async_generator", "awaitable", "value", "failure"]
)
def test_worker_close_rejects_invalid_driver_close_results(result_kind: str) -> None:
    async def coroutine_result() -> None:
        return None

    async def generator_result():  # type: ignore[no-untyped-def]
        yield None

    class AwaitableResult:
        def __await__(self):  # type: ignore[no-untyped-def]
            yield

    def close_result() -> object:
        if result_kind == "coroutine":
            return coroutine_result()
        if result_kind == "async_generator":
            return generator_result()
        if result_kind == "awaitable":
            return AwaitableResult()
        if result_kind == "failure":
            raise RuntimeError("close")
        return "unexpected"

    kernel = WorkerKernel(_Driver(), driver_close=close_result)
    with pytest.raises((TypeError, RuntimeError)):
        kernel.close()
    assert kernel.state is not worker_module.WorkerState.STOPPED


def test_resource_permit_renewal_loop_records_failure() -> None:
    class DormantThread:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def start(self) -> None:
            pass

        def join(self) -> None:
            pass

    service = _service()
    permit = FencedResourceLeasePermit(
        service,
        _active_lease(service),
        authorization_digest="auth",
        renewal_interval=timedelta(seconds=1),
        thread_factory=DormantThread,  # type: ignore[arg-type]
    )
    permit._stop = Mock()
    permit._stop.wait.return_value = False
    permit._stop.is_set.return_value = False
    permit.renew = Mock(side_effect=RuntimeError("renewal"))  # type: ignore[method-assign]
    permit._renew_loop()
    assert isinstance(permit._failure, RuntimeError)
    permit._failure = None
    permit._stop.is_set.return_value = True
    permit._renew_loop()
    assert permit._failure is None


@pytest.mark.parametrize(
    "call",
    [
        lambda: queue_contracts._identifier("INVALID", "value"),
        lambda: queue_contracts._aware_utc(datetime(2026, 1, 1), "time"),
        lambda: queue_contracts._duration(timedelta(0), "duration"),
        lambda: queue_contracts._opaque_tag("bad\nvalue", "tag"),
        lambda: QueueJobTags(tenant_ref="invalid"),
        lambda: QueueJobTags.from_envelope(object()),
        lambda: QueueDeadLetter(object(), 1, NOW),
        lambda: QueueDeadLetterReplayReceipt("message", 0, NOW),
        lambda: QueueReservation(object(), 1, NOW),
    ],
)
def test_queue_worker_contract_helpers_reject_invalid_boundaries(call: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        call()  # type: ignore[operator]


def test_package_entrypoint_invokes_console_app(monkeypatch: pytest.MonkeyPatch) -> None:
    invoked = Mock()
    monkeypatch.setattr("af.interfaces.cli.app", invoked)
    runpy.run_module("af.__main__", run_name="__main__")
    invoked.assert_called_once_with()
    runpy.run_module("af.__main__", run_name="af.__main__.qualification")
    invoked.assert_called_once_with()


def test_worker_optimization_rejects_naive_current_time(tmp_path: Path) -> None:
    with pytest.raises(InvalidProviderConfiguration):
        record_worker_optimization_profile(
            tmp_path / "profile.json",
            _qualification(),
            now=datetime(2026, 8, 18),
        )


def _queue_job(message_id: str = "message") -> QueueJob:
    return QueueJob(
        MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=NOW,
            ),
            payload={"message_id": message_id},
        )
    )


@pytest.mark.parametrize(
    "call",
    [
        lambda: QueueDeadLetter(_queue_job(), 0, NOW),
        lambda: QueueJob(object()),
        lambda: QueueJob(_queue_job().envelope, tags=object()),
        lambda: QueueReservation(_queue_job(), 1, NOW, fencing_token=0),
        lambda: QueueReservation(_queue_job(), 1, NOW, owner_id="owner"),
        lambda: QueueReservation(
            _queue_job(),
            1,
            NOW,
            owner_id="a" * 64,
            reservation_id="b" * 64,
            connection_name="primary",
        ),
        lambda: QueueReservation(
            _queue_job(),
            1,
            NOW,
            owner_id="a" * 64,
            reservation_id="b" * 64,
            connection_name="primary",
            connection_binding="invalid",
        ),
    ],
)
def test_queue_delivery_contracts_reject_invalid_boundaries(call: object) -> None:
    with pytest.raises(ValueError):
        call()  # type: ignore[operator]


def test_renewal_coordinator_rejects_unsupported_drivers() -> None:
    reservation = QueueReservation(
        _queue_job(),
        1,
        NOW,
        expires_at=NOW + timedelta(minutes=1),
    )
    with pytest.raises(worker_module.QueueOperationError):
        worker_module._RenewalCoordinator(
            object(),
            reservation,
            clock=lambda: NOW,
            minimum_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=5),
        )

    class UnsupportedRenewalDriver:
        def renew(self, current: QueueReservation) -> QueueReservation:
            return current

        def supports(self, feature: object) -> bool:
            del feature
            return False

    with pytest.raises(worker_module.QueueOperationError):
        worker_module._RenewalCoordinator(
            UnsupportedRenewalDriver(),
            reservation,
            clock=lambda: NOW,
            minimum_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=5),
        )
