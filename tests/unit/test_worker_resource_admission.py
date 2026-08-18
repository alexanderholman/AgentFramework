from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock, Thread
from unittest.mock import Mock

import pytest

from af.application.resource_leases import ResourceLeaseService
from af.application.worker_resource_admission import FencedResourceLeaseAdmissionGate
from af.domain.resources import (
    FenceToken,
    LeaseState,
    ResourceBudget,
    ResourceIdentity,
    ResourceKind,
)
from af.framework.errors import (
    LeaseBudgetExceeded,
    LeaseConcurrencyExceeded,
    LeaseFenceTokenStale,
    LeaseStateTransitionError,
)
from af.infrastructure.resource_leases import InMemoryLeaseRepository
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import QueueJob, QueueReservation, WorkerKernel

_NOW = datetime(2026, 8, 17, 18, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, duration: timedelta) -> None:
        self.now += duration


def _budget() -> ResourceBudget:
    return ResourceBudget(
        identity=ResourceIdentity(
            kind=ResourceKind.CONCURRENCY,
            host_id="host.local",
            instance_id="worker.slots",
        ),
        max_quantity=2,
        max_duration=timedelta(minutes=2),
        max_concurrent=2,
    )


def _job(message_id: str) -> QueueJob:
    return QueueJob(
        MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW,
            ),
            payload={"secret": "redacted"},
        )
    )


def test_resource_lease_service_requires_exact_fence_and_atomic_create() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    created = service.create_lease(
        "lease-exact",
        _budget(),
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )

    with pytest.raises(LeaseFenceTokenStale):
        service.activate_lease(
            created.id,
            FenceToken(value=2, created_at=_NOW),
            "authorization-v1",
        )
    with pytest.raises(LeaseFenceTokenStale):
        service.create_lease(
            created.id,
            _budget(),
            1,
            {"queue": "default"},
            "worker-policy",
            "authorization-v1",
        )

    assert service.get_lease(created.id) == created


def test_resource_lease_compare_and_swap_allows_only_one_same_fence_renewal() -> None:
    repository = InMemoryLeaseRepository()
    clock = _Clock()
    service = ResourceLeaseService(repository, clock=clock)
    created = service.create_lease(
        "lease-race",
        _budget(),
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )
    assert created.fence_token is not None
    active = service.activate_lease(
        created.id,
        created.fence_token,
        "authorization-v1",
    )
    assert active.fence_token is not None
    active_fence = active.fence_token
    barrier = Barrier(3)
    lock = Lock()
    outcomes: list[str] = []

    def renew() -> None:
        barrier.wait()
        try:
            service.renew_lease(
                active.id,
                active_fence,
                authorization_digest="authorization-v1",
            )
        except LeaseFenceTokenStale:
            outcome = "stale"
        else:
            outcome = "renewed"
        with lock:
            outcomes.append(outcome)

    workers = [Thread(target=renew) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert sorted(outcomes) == ["renewed", "stale"]
    final = service.get_lease(active.id)
    assert final.fence_token is not None
    assert final.fence_token.value == 3


def test_resource_lease_create_atomically_enforces_aggregate_capacity() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    barrier = Barrier(3)
    lock = Lock()
    outcomes: list[str] = []

    def create(lease_id: str) -> None:
        barrier.wait()
        try:
            service.create_lease(
                lease_id,
                _budget(),
                2,
                {"queue": "default"},
                "worker-policy",
                "authorization-v1",
            )
        except LeaseBudgetExceeded:
            outcome = "capacity-rejected"
        else:
            outcome = "reserved"
        with lock:
            outcomes.append(outcome)

    workers = [Thread(target=create, args=(f"lease-capacity-{index}",)) for index in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=2)

    assert sorted(outcomes) == ["capacity-rejected", "reserved"]
    consuming = [
        lease
        for lease in repository.list(host_id="host.local")
        if lease.state in {LeaseState.RESERVED, LeaseState.ACTIVE}
    ]
    assert len(consuming) == 1
    assert sum(lease.quantity for lease in consuming) == 2


def test_resource_lease_create_atomically_enforces_aggregate_concurrency() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    budget = ResourceBudget(
        identity=_budget().identity,
        max_quantity=2,
        max_duration=timedelta(minutes=2),
        max_concurrent=1,
    )
    service.create_lease(
        "lease-concurrency-1",
        budget,
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )

    with pytest.raises(LeaseConcurrencyExceeded) as exceeded:
        service.create_lease(
            "lease-concurrency-2",
            budget,
            1,
            {"queue": "default"},
            "worker-policy",
            "authorization-v1",
        )

    assert exceeded.value.lease_id == "lease-concurrency-2"
    assert exceeded.value.requested == 2
    assert exceeded.value.max_concurrent == 1
    assert repository.get("lease-concurrency-2") is None


def test_resource_lease_create_cannot_widen_a_consuming_identity_budget() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    strict_budget = ResourceBudget(
        identity=_budget().identity,
        max_quantity=2,
        max_duration=timedelta(minutes=2),
        max_concurrent=1,
    )
    service.create_lease(
        "lease-strict",
        strict_budget,
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )
    widened_budget = ResourceBudget(
        identity=strict_budget.identity,
        max_quantity=4,
        max_duration=strict_budget.max_duration,
        max_concurrent=2,
    )

    with pytest.raises(LeaseConcurrencyExceeded) as exceeded:
        service.create_lease(
            "lease-widened",
            widened_budget,
            1,
            {"queue": "default"},
            "worker-policy",
            "authorization-v1",
        )

    assert exceeded.value.max_concurrent == 1
    assert repository.get("lease-widened") is None


def test_fenced_resource_admission_releases_and_reconciles_after_worker_job() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    lease_ids = iter(("worker-lease-1",))
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: next(lease_ids),
    )
    job = _job("job-fenced-admission")
    reservation = QueueReservation(job=job, attempt=1, reserved_at=_NOW)
    driver = Mock()
    driver.reserve.return_value = reservation

    processed = WorkerKernel(driver, admission_gate=gate, clock=lambda: _NOW).run_once(
        lambda _job: None,
        lane="default",
    )

    assert processed == reservation
    driver.ack.assert_called_once_with(reservation)
    lease = service.get_lease("worker-lease-1")
    assert lease.state is LeaseState.RECONCILED
    assert lease.released_at == _NOW
    assert lease.fence_token is not None
    assert lease.fence_token.value == 4
    assert lease.scope == {"host": "host.local", "queue_lane": "default"}


def test_fenced_resource_permit_renews_with_authorization_and_latest_fence() -> None:
    repository = InMemoryLeaseRepository()
    clock = _Clock()
    service = ResourceLeaseService(repository, clock=clock)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-renew",
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    clock.advance(timedelta(minutes=1))

    renewed = permit.renew()
    permit.release()

    assert renewed.renewed_count == 1
    assert renewed.expires_at == _NOW + timedelta(minutes=3)
    final = service.get_lease("worker-lease-renew")
    assert final.state is LeaseState.RECONCILED
    assert final.fence_token is not None
    assert final.fence_token.value == 5


def test_fenced_resource_permit_cannot_renew_at_heartbeat_expiry() -> None:
    repository = InMemoryLeaseRepository()
    clock = _Clock()
    service = ResourceLeaseService(repository, clock=clock)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-expired-heartbeat",
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    original = permit.lease
    assert original.expires_at is not None
    clock.advance(original.expires_at - clock.now)

    with pytest.raises(LeaseStateTransitionError) as expired:
        permit.renew()

    assert expired.value.lease_id == original.id
    assert expired.value.from_state == LeaseState.ACTIVE.value
    assert expired.value.to_state == LeaseState.EXPIRED.value
    assert service.get_lease(original.id) == original


def test_fenced_resource_admission_cleans_up_when_permit_startup_fails() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)

    def fail_thread(**_: object) -> Thread:
        raise RuntimeError("thread unavailable")

    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-startup-failure",
        thread_factory=fail_thread,
    )

    with pytest.raises(RuntimeError, match="thread unavailable"):
        gate(lane="default", active_jobs=0, max_concurrency=2)

    final = service.get_lease("worker-lease-startup-failure")
    assert final.state is LeaseState.RECONCILED
    assert final.released_at == _NOW
    assert final.fence_token is not None
    assert final.fence_token.value == 4
