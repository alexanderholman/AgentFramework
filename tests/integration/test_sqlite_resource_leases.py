from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Protocol

import pytest

from af.application.resource_leases import ResourceLeaseService
from af.domain.resources import LeaseState, ResourceBudget, ResourceIdentity, ResourceKind
from af.framework.errors import LeaseBudgetExceeded, LeaseFenceTokenStale, LeaseStateTransitionError
from af.infrastructure.sqlite import SQLiteDriver
from af.infrastructure.sqlite_resource_leases import SQLiteLeaseRepository

_NOW = datetime(2026, 8, 18, tzinfo=UTC)


def _budget() -> ResourceBudget:
    return ResourceBudget(
        identity=ResourceIdentity(
            kind=ResourceKind.CONCURRENCY,
            host_id="host.local",
            instance_id="worker.slots",
            topology={"zone": "local"},
        ),
        max_quantity=2,
        max_duration=timedelta(minutes=2),
        max_concurrent=2,
    )


def _service(
    driver: SQLiteDriver,
) -> tuple[ResourceLeaseService, SQLiteLeaseRepository]:
    repository = SQLiteLeaseRepository(driver)
    return ResourceLeaseService(repository, clock=lambda: _NOW), repository


class _ProcessBarrier(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...


class _ResultQueue(Protocol):
    def put(self, value: str) -> None: ...


def _reserve_in_process(
    database: str,
    barrier: _ProcessBarrier,
    outcomes: _ResultQueue,
    lease_id: str,
) -> None:
    driver = SQLiteDriver(database)
    service, _ = _service(driver)
    barrier.wait(timeout=10)
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
        outcomes.put("capacity-rejected")
    else:
        outcomes.put("reserved")
    finally:
        driver.close()


def test_sqlite_resource_lease_persists_full_lifecycle_across_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-leases.sqlite3"
    first_driver = SQLiteDriver(database)
    first, _ = _service(first_driver)
    created = first.create_lease(
        "lease-persistent",
        _budget(),
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
        invocation_id="invocation-1",
        capability_digest="sha256:capability",
        runtime_digest="sha256:runtime",
    )
    assert created.fence_token is not None
    active = first.activate_lease(
        created.id,
        created.fence_token,
        authorization_digest="authorization-v1",
    )
    first_driver.close()

    second_driver = SQLiteDriver(database)
    second, second_repository = _service(second_driver)

    assert second.get_lease(active.id) == active
    assert second_repository.list(host_id="host.local", state=LeaseState.ACTIVE.value) == [active]
    second_driver.close()


def test_sqlite_resource_lease_admission_is_atomic_across_connections(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-lease-contention.sqlite3"
    first_driver = SQLiteDriver(database)
    second_driver = SQLiteDriver(database)
    first, first_repository = _service(first_driver)
    second, _ = _service(second_driver)
    barrier = Barrier(3)
    lock = Lock()
    outcomes: list[str] = []

    def create(service: ResourceLeaseService, lease_id: str) -> None:
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

    workers = [
        Thread(target=create, args=(first, "lease-first")),
        Thread(target=create, args=(second, "lease-second")),
    ]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert sorted(outcomes) == ["capacity-rejected", "reserved"]
    assert len(first_repository.list(host_id="host.local")) == 1
    first_driver.close()
    second_driver.close()


def test_sqlite_resource_lease_admission_is_atomic_across_spawned_processes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-lease-process-contention.sqlite3"
    installer = SQLiteDriver(database)
    SQLiteLeaseRepository(installer)
    installer.close()
    context = get_context("spawn")
    barrier = context.Barrier(3)
    outcomes = context.Queue()
    workers = [
        context.Process(
            target=_reserve_in_process,
            args=(str(database), barrier, outcomes, f"lease-process-{index}"),
        )
        for index in range(2)
    ]

    for worker in workers:
        worker.start()
    barrier.wait(timeout=10)
    for worker in workers:
        worker.join(timeout=10)

    assert [worker.exitcode for worker in workers] == [0, 0]
    assert sorted((outcomes.get(timeout=2), outcomes.get(timeout=2))) == [
        "capacity-rejected",
        "reserved",
    ]
    outcomes.close()


def test_sqlite_resource_lease_sweep_fences_crashed_owners_and_releases_capacity(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-lease-sweep.sqlite3"
    first_driver = SQLiteDriver(database)
    first, first_repository = _service(first_driver)
    reserved = first.create_lease(
        "lease-reserved-crash",
        _budget(),
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )
    reserved = replace(reserved, expires_at=None)
    first_repository.save(reserved)
    active_candidate = first.create_lease(
        "lease-active-crash",
        _budget(),
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )
    assert active_candidate.fence_token is not None
    active = first.activate_lease(active_candidate.id, active_candidate.fence_token)
    first_driver.close()

    second_driver = SQLiteDriver(database)
    sweep_time = _NOW + timedelta(minutes=2)
    second = ResourceLeaseService(SQLiteLeaseRepository(second_driver), clock=lambda: sweep_time)

    assert reserved.fence_token is not None
    with pytest.raises(LeaseStateTransitionError):
        second.activate_lease(reserved.id, reserved.fence_token)
    assert second.get_lease(reserved.id) == reserved

    result = second.sweep_expired_leases(host_id="host.local")

    assert result.expired_lease_ids == (active.id, reserved.id)
    assert result.reconciled_lease_ids == (active.id, reserved.id)
    assert result.conflicted_lease_ids == ()
    for lease_id in result.reconciled_lease_ids:
        reconciled = second.get_lease(lease_id)
        assert reconciled.state is LeaseState.RECONCILED
        assert reconciled.fence_token is not None
        assert reconciled.fence_token.value == (4 if lease_id == active.id else 3)
    assert active.fence_token is not None
    with pytest.raises(LeaseFenceTokenStale):
        second.renew_lease(active.id, active.fence_token)

    replacement = second.create_lease(
        "lease-replacement",
        _budget(),
        2,
        {"queue": "default"},
        "worker-policy",
        "authorization-v2",
    )
    assert replacement.state is LeaseState.RESERVED
    second_driver.close()


def test_sqlite_resource_lease_sweep_finishes_an_interrupted_reconciliation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "resource-lease-interrupted-reconciliation.sqlite3"
    first_driver = SQLiteDriver(database)
    first, _ = _service(first_driver)
    reserved = first.create_lease(
        "lease-expired-before-crash",
        _budget(),
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )
    assert reserved.fence_token is not None
    expired = first.expire_lease(reserved.id, reserved.fence_token)
    first_driver.close()

    second_driver = SQLiteDriver(database)
    second, _ = _service(second_driver)
    result = second.sweep_expired_leases(host_id="host.local")

    assert result.expired_lease_ids == ()
    assert result.reconciled_lease_ids == (expired.id,)
    assert result.conflicted_lease_ids == ()
    reconciled = second.get_lease(expired.id)
    assert reconciled.state is LeaseState.RECONCILED
    assert reconciled.fence_token is not None
    assert reconciled.fence_token.value == 3
    second_driver.close()
