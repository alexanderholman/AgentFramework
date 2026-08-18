from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from af.application.worker_generation import (
    PinnedWorkerOptimizationProvider,
    WorkerGenerationFactory,
    WorkerOptimizationContext,
    WorkerRuntimeOptimization,
)
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)
from af.queue.contracts import QueueDispatchDisposition, QueueDispatchReceipt, QueueJob
from af.queue.manager import QueueDriver, QueueFeature, QueueManager

_NOW = datetime(2026, 8, 18, 8, tzinfo=UTC)


class _Driver:
    features = frozenset({QueueFeature.RESERVATION, QueueFeature.RETRY})

    def __init__(self, lifecycle: list[str]) -> None:
        self._lifecycle = lifecycle

    def supports(self, feature: QueueFeature) -> bool:
        return feature in self.features

    def dispatch(self, job: QueueJob) -> QueueDispatchReceipt:
        return QueueDispatchReceipt(
            message_id=job.envelope.metadata.message_id,
            lane=job.lane,
            disposition=QueueDispatchDisposition.ENQUEUED,
            dispatched_at=_NOW,
        )

    def reserve(self, *, lane: str | None = None):  # type: ignore[no-untyped-def]
        del lane
        return None

    def ack(self, reservation, /) -> None:  # type: ignore[no-untyped-def]
        del reservation

    def reject(self, reservation, /) -> None:  # type: ignore[no-untyped-def]
        del reservation

    def release(self, reservation, /) -> None:  # type: ignore[no-untyped-def]
        del reservation

    def retry(self, reservation, /, *, available_at=None) -> None:  # type: ignore[no-untyped-def]
        del reservation, available_at

    def close(self) -> None:
        self._lifecycle.append("driver.close")


def _registration(lifecycle: list[str]) -> DriverRegistration[QueueDriver]:
    capabilities = CapabilitySet((QueueFeature.RESERVATION.value, QueueFeature.RETRY.value))
    manifest = DriverManifest(
        driver_name="scripted",
        version="1.0.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        capabilities=capabilities,
    )

    def verify(ref, driver, binding):  # type: ignore[no-untyped-def]
        assert ref.driver_name == "scripted"
        assert isinstance(driver, _Driver)
        return CapabilityEvidence(
            binding=binding,
            capabilities=capabilities,
            health=HealthStatus.HEALTHY,
            observed_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
            evidence_digest="c" * 64,
        )

    return cast(
        DriverRegistration[QueueDriver],
        DriverRegistration(
            manager_id="queue",
            driver_name="scripted",
            factory=lambda _ref, _config: _Driver(lifecycle),
            manifest=manifest,
            verifier=verify,
            disposer=lambda driver: driver.close(),
        ),
    )


def _manager_factory(
    lifecycle: list[str],
    created: list[QueueManager],
) -> Callable[[], QueueManager]:
    registration = _registration(lifecycle)

    def create() -> QueueManager:
        manager = QueueManager(
            connections={"primary": {"driver": "scripted"}},
            default="primary",
            deployment_profile="test",
            policy_version="policy-v1",
            clock=lambda: _NOW,
        )
        manager.install(registration)
        created.append(manager)
        return manager

    return create


def _optimization(*, workers: int = 2) -> WorkerRuntimeOptimization:
    return WorkerRuntimeOptimization(
        worker_count=workers,
        max_concurrency=workers,
        poll_interval=timedelta(milliseconds=20),
        heartbeat_interval=timedelta(seconds=5),
        max_idle_time=timedelta(minutes=2),
        queue_wait_alert_after=timedelta(seconds=3),
        renewal_min_interval=timedelta(milliseconds=100),
        renewal_max_interval=timedelta(seconds=10),
    )


def test_worker_generation_uses_verified_backend_optimization_and_manager_close() -> None:
    lifecycle: list[str] = []
    created: list[QueueManager] = []
    contexts: list[WorkerOptimizationContext] = []

    def optimize(context: WorkerOptimizationContext) -> WorkerRuntimeOptimization:
        contexts.append(context)
        return _optimization()

    generation = WorkerGenerationFactory(
        _manager_factory(lifecycle, created),
        optimizers={"scripted": optimize},
    )

    runtime = generation.create(lambda job: None, lane="priority")

    assert len(contexts) == 1
    assert contexts[0].reference.driver_name == "scripted"
    assert contexts[0].reference.connection_name == "primary"
    assert contexts[0].evidence == created[0].evidence()
    assert contexts[0].evidence.capabilities.supports((QueueFeature.RESERVATION.value,))
    assert runtime.worker_count == 2
    assert runtime.max_concurrency == 2
    assert runtime.lane == "priority"
    assert runtime.poll_interval == timedelta(milliseconds=20)

    runtime.close()
    runtime.close()

    assert lifecycle == ["driver.close"]


def test_worker_generation_fails_closed_without_backend_optimizer_and_rolls_back() -> None:
    lifecycle: list[str] = []
    created: list[QueueManager] = []
    generation = WorkerGenerationFactory(
        _manager_factory(lifecycle, created),
        optimizers={},
    )

    with pytest.raises(LookupError, match="optimization provider"):
        generation.create(lambda job: None)

    assert lifecycle == ["driver.close"]
    assert len(created) == 1


def test_worker_generation_rejects_borrowed_resolved_manager_without_closing_it() -> None:
    lifecycle: list[str] = []
    created: list[QueueManager] = []
    manager = _manager_factory(lifecycle, created)()
    manager.connection(requires=(QueueFeature.RESERVATION,))
    generation = WorkerGenerationFactory(
        lambda: manager,
        optimizers={"scripted": lambda context: _optimization(workers=1)},
    )

    with pytest.raises(ValueError, match="fresh QueueManager"):
        generation.create(lambda job: None)

    assert lifecycle == []
    assert manager.evidence().health is HealthStatus.HEALTHY
    manager.close()
    assert lifecycle == ["driver.close"]


def test_worker_generation_composes_only_explicit_non_queue_manager_recycling() -> None:
    lifecycle: list[str] = []
    created: list[QueueManager] = []

    class OwnedManager:
        def purge(self) -> None:
            lifecycle.append("handler.purge")

    generation = WorkerGenerationFactory(
        _manager_factory(lifecycle, created),
        optimizers={"scripted": lambda context: _optimization(workers=1)},
    )
    runtime = generation.create(
        lambda job: None,
        owned_connection_managers=(OwnedManager(),),
    )

    runtime.start()
    runtime.close(timeout=timedelta(seconds=1))

    assert lifecycle == ["driver.close"]

    with pytest.raises(ValueError, match="queue manager"):
        generation.create(
            lambda job: None,
            owned_connection_managers=(created[0],),
        )


def test_pinned_worker_optimizer_rejects_binding_drift() -> None:
    lifecycle: list[str] = []
    created: list[QueueManager] = []
    manager = _manager_factory(lifecycle, created)()
    manager.connection(requires=(QueueFeature.RESERVATION,))
    evidence = manager.evidence()
    provider = PinnedWorkerOptimizationProvider(
        driver_name="scripted",
        binding=evidence.binding,
        profile_digest="d" * 64,
        qualification_digest="e" * 64,
        optimization=_optimization(workers=1),
    )
    context = WorkerOptimizationContext(reference=manager.reference(), evidence=evidence)

    assert provider(context) == _optimization(workers=1)

    wrong_backend = WorkerOptimizationContext(
        reference=ConnectionRef("queue", "primary", "other"),
        evidence=evidence,
    )
    with pytest.raises(ValueError, match="backend"):
        provider(wrong_backend)

    changed = CapabilityBinding(
        driver_version=evidence.binding.driver_version,
        artifact_digest=evidence.binding.artifact_digest,
        dependency_graph_digest=evidence.binding.dependency_graph_digest,
        configuration_digest="e" * 64,
        deployment_profile=evidence.binding.deployment_profile,
        policy_version=evidence.binding.policy_version,
    )
    drifted = WorkerOptimizationContext(
        reference=manager.reference(),
        evidence=CapabilityEvidence(
            binding=changed,
            capabilities=evidence.capabilities,
            health=evidence.health,
            observed_at=evidence.observed_at,
            expires_at=evidence.expires_at,
            evidence_digest="f" * 64,
        ),
    )

    with pytest.raises(ValueError, match="binding"):
        provider(drifted)

    manager.close()
