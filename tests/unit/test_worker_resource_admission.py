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


class _DormantThread:
    def start(self) -> None:
        return

    def join(self) -> None:
        return


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


@pytest.mark.parametrize("capacity_kind", ["quantity", "concurrency"])
def test_fenced_resource_admission_treats_aggregate_saturation_as_denial(
    capacity_kind: str,
) -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    budget = _budget()
    existing_quantity = 2
    if capacity_kind == "concurrency":
        budget = ResourceBudget(
            identity=budget.identity,
            max_quantity=2,
            max_duration=budget.max_duration,
            max_concurrent=1,
        )
        existing_quantity = 1
    service.create_lease(
        "existing-lease",
        budget,
        existing_quantity,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=budget,
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "denied-lease",
    )

    assert gate(lane="default", active_jobs=0, max_concurrency=2) is False
    assert repository.get("denied-lease") is None


@pytest.mark.parametrize(
    ("active_jobs", "max_concurrency", "message"),
    [
        (-1, 2, "active-job count"),
        (False, 2, "active-job count"),
        (0, 0, "maximum concurrency"),
        (0, True, "maximum concurrency"),
    ],
)
def test_fenced_resource_admission_rejects_invalid_worker_capacity_facts(
    active_jobs: int,
    max_concurrency: int,
    message: str,
) -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "invalid-capacity-lease",
    )

    with pytest.raises(ValueError, match=message):
        gate(
            lane="default",
            active_jobs=active_jobs,
            max_concurrency=max_concurrency,
        )

    assert repository.get("invalid-capacity-lease") is None


def test_fenced_resource_admission_rejects_oversized_configured_quantity() -> None:
    service = ResourceLeaseService(InMemoryLeaseRepository(), clock=lambda: _NOW)

    with pytest.raises(ValueError, match="must not exceed budget maximum"):
        FencedResourceLeaseAdmissionGate(
            service,
            budget=_budget(),
            quantity=3,
            scope={},
            authorized_by="worker-policy",
            authorization_digest="authorization-v1",
        )


@pytest.mark.parametrize("maximum", [float("nan"), float("inf"), float("-inf")])
def test_fenced_resource_admission_rejects_non_finite_budget_maximum(
    maximum: float,
) -> None:
    service = ResourceLeaseService(InMemoryLeaseRepository(), clock=lambda: _NOW)
    budget = ResourceBudget(identity=_budget().identity, max_quantity=maximum)

    with pytest.raises(ValueError, match="budget maximum must be finite and positive"):
        FencedResourceLeaseAdmissionGate(
            service,
            budget=budget,
            quantity=1,
            scope={},
            authorized_by="worker-policy",
            authorization_digest="authorization-v1",
        )


def test_fenced_resource_admission_propagates_non_capacity_create_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ResourceLeaseService(InMemoryLeaseRepository(), clock=lambda: _NOW)
    failure = RuntimeError("lease repository unavailable")
    monkeypatch.setattr(service, "create_lease", Mock(side_effect=failure))
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "failed-lease",
    )

    with pytest.raises(RuntimeError) as raised:
        gate(lane=None, active_jobs=0, max_concurrency=2)

    assert raised.value is failure


def test_fenced_resource_admission_recovers_uncertain_persisted_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    activate_lease = service.activate_lease

    def committed_activation_then_fail(
        lease_id: str,
        fence_token: FenceToken,
        authorization_digest: str | None = None,
    ) -> None:
        activate_lease(lease_id, fence_token, authorization_digest)
        raise RuntimeError("uncertain activation result")

    monkeypatch.setattr(service, "activate_lease", committed_activation_then_fail)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-uncertain-activation",
    )

    with pytest.raises(RuntimeError, match="uncertain activation result"):
        gate(lane="default", active_jobs=0, max_concurrency=2)

    final = service.get_lease("worker-lease-uncertain-activation")
    assert final.state is LeaseState.RECONCILED
    assert final.fence_token is not None
    assert final.fence_token.value == 4


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


def test_fenced_resource_permit_accepts_externally_reconciled_cleanup() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-external-reconcile",
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    active = permit.lease
    assert active.fence_token is not None
    expired = service.expire_lease(active.id, active.fence_token)
    assert expired.fence_token is not None
    reconciled = service.reconcile_lease(expired.id, expired.fence_token)

    permit.release()

    assert permit.lease == reconciled
    assert permit.lease.state is LeaseState.RECONCILED


def test_fenced_resource_permit_reconciles_externally_expired_cleanup() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-external-expire",
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    active = permit.lease
    assert active.fence_token is not None
    expired = service.expire_lease(active.id, active.fence_token)

    permit.release()

    reconciled = service.get_lease(expired.id)
    assert permit.lease == reconciled
    assert reconciled.state is LeaseState.RECONCILED


def test_fenced_resource_permit_reconciles_externally_revoked_cleanup() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-external-revoke",
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    active = permit.lease
    assert active.fence_token is not None
    revoked = service.revoke_lease(active.id, active.fence_token)

    permit.release()

    reconciled = service.get_lease(revoked.id)
    assert permit.lease == reconciled
    assert reconciled.state is LeaseState.RECONCILED


def test_fenced_resource_permit_accepts_revocation_winning_renewal_race() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-revoke-renewal-race",
        renewal_interval=timedelta(milliseconds=1),
        thread_factory=lambda **_: _DormantThread(),
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    active = permit.lease
    assert active.fence_token is not None
    service.revoke_lease(active.id, active.fence_token)

    permit._renew_loop()
    permit.release()

    assert permit.lease.state is LeaseState.RECONCILED


def test_fenced_resource_permit_preserves_unrelated_failure_after_revocation() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-revoke-renewal-failure",
        renewal_interval=timedelta(milliseconds=1),
        thread_factory=lambda **_: _DormantThread(),
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    active = permit.lease
    assert active.fence_token is not None
    service.revoke_lease(active.id, active.fence_token)
    failure = RuntimeError("renewal transport failed")

    def fail_renewal(*_: object, **__: object) -> None:
        raise failure

    service.renew_lease = fail_renewal  # type: ignore[method-assign]
    permit._renew_loop()

    with pytest.raises(RuntimeError, match="renewal transport failed") as raised:
        permit.release()
    assert raised.value is failure
    assert permit.lease.state is LeaseState.RECONCILED


def test_fenced_resource_permit_resumes_from_uncertain_persisted_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-uncertain-release",
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    release_lease = service.release_lease
    attempts = 0

    def committed_release_then_fail(lease_id: str, fence_token: FenceToken):  # type: ignore[no-untyped-def]
        nonlocal attempts
        attempts += 1
        release_lease(lease_id, fence_token)
        raise RuntimeError("uncertain release result")

    monkeypatch.setattr(service, "release_lease", committed_release_then_fail)
    with pytest.raises(RuntimeError, match="uncertain release result"):
        permit.release()
    assert service.get_lease(permit.lease.id).state is LeaseState.RELEASED

    permit.release()

    assert attempts == 1
    assert permit.lease.state is LeaseState.RECONCILED


def test_fenced_resource_permit_records_renewal_failure_after_concurrent_stop() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    failure = LeaseFenceTokenStale("worker-lease-renewal-stop-race", 3, 2)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-renewal-stop-race",
        renewal_interval=timedelta(milliseconds=1),
        thread_factory=lambda **_: _DormantThread(),
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)

    def fail_after_stop(*_: object, **__: object) -> None:
        permit._stop.set()
        raise failure

    service.renew_lease = fail_after_stop  # type: ignore[method-assign]
    permit._renew_loop()

    with pytest.raises(LeaseFenceTokenStale) as raised:
        permit.release()
    assert raised.value is failure


def test_fenced_resource_permit_rejects_renewal_after_release_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-release-renewal-race",
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    release_loaded = Barrier(2)
    continue_release = Barrier(2)
    get_lease = service.get_lease
    release_failures: list[BaseException] = []

    def pause_after_release_load(lease_id: str):  # type: ignore[no-untyped-def]
        persisted = get_lease(lease_id)
        release_loaded.wait(timeout=2)
        continue_release.wait(timeout=2)
        return persisted

    def release() -> None:
        try:
            permit.release()
        except BaseException as error:
            release_failures.append(error)

    monkeypatch.setattr(service, "get_lease", pause_after_release_load)
    release_thread = Thread(target=release)
    release_thread.start()
    release_loaded.wait(timeout=2)
    try:
        with pytest.raises(RuntimeError, match="permit is releasing"):
            permit.renew()
    finally:
        continue_release.wait(timeout=2)
        release_thread.join(timeout=2)

    assert not release_thread.is_alive()
    assert release_failures == []
    assert permit.lease.state is LeaseState.RECONCILED


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


def test_fenced_resource_admission_stops_uncertain_started_renewal_thread() -> None:
    class UncertainStartThread(Thread):
        def start(self) -> None:
            super().start()
            raise RuntimeError("uncertain renewal thread startup")

    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    threads: list[UncertainStartThread] = []
    permit_owners: list[object] = []

    def uncertain_thread_factory(**kwargs: object) -> Thread:
        target = kwargs["target"]
        permit_owners.append(target.__self__)  # type: ignore[union-attr]
        thread = UncertainStartThread(**kwargs)  # type: ignore[arg-type]
        threads.append(thread)
        return thread

    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-uncertain-thread-startup",
        renewal_interval=timedelta(minutes=1),
        thread_factory=uncertain_thread_factory,
    )

    with pytest.raises(RuntimeError, match="uncertain renewal thread startup"):
        gate(lane="default", active_jobs=0, max_concurrency=2)

    renewal_thread = threads[0]
    try:
        assert not renewal_thread.is_alive()
    finally:
        permit_owners[0]._stop.set()  # type: ignore[attr-defined]
        renewal_thread.join(timeout=2)

    final = service.get_lease("worker-lease-uncertain-thread-startup")
    assert final.state is LeaseState.RECONCILED


def test_fenced_resource_permit_does_not_release_lease_after_fence_moves() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-fence-moved",
        renewal_interval=timedelta(minutes=1),
        thread_factory=lambda **_: _DormantThread(),
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    owned = permit.lease
    assert owned.fence_token is not None
    advanced = service.renew_lease(
        owned.id, owned.fence_token, authorization_digest="authorization-v1"
    )
    assert advanced.fence_token is not None
    permit._failure = LeaseFenceTokenStale(
        owned.id, advanced.fence_token.value, owned.fence_token.value
    )

    with pytest.raises(LeaseFenceTokenStale):
        permit.release()

    persisted = service.get_lease(owned.id)
    assert persisted.state is LeaseState.ACTIVE
    assert persisted.fence_token == advanced.fence_token


def test_fenced_resource_permit_relinquishes_uncertain_advanced_fence() -> None:
    repository = InMemoryLeaseRepository()
    service = ResourceLeaseService(repository, clock=lambda: _NOW)
    gate = FencedResourceLeaseAdmissionGate(
        service,
        budget=_budget(),
        quantity=1,
        scope={"host": "host.local"},
        authorized_by="worker-policy",
        authorization_digest="authorization-v1",
        lease_id_factory=lambda: "worker-lease-uncertain-advanced-fence",
        renewal_interval=timedelta(minutes=1),
        thread_factory=lambda **_: _DormantThread(),
    )
    permit = gate(lane="default", active_jobs=0, max_concurrency=2)
    assert not isinstance(permit, bool)
    owned = permit.lease
    assert owned.fence_token is not None
    advanced = service.renew_lease(
        owned.id, owned.fence_token, authorization_digest="authorization-v1"
    )
    assert advanced.fence_token is not None
    failure = RuntimeError("renewal committed but response was lost")
    permit._failure = failure

    with pytest.raises(RuntimeError, match="response was lost") as raised:
        permit.release()

    assert raised.value is failure
    persisted = service.get_lease(owned.id)
    assert persisted.state is LeaseState.ACTIVE
    assert persisted.fence_token == advanced.fence_token
    permit.release()
    assert service.get_lease(owned.id) == persisted
