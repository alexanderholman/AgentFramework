from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Any

import pytest

from af.framework.errors import PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilitySet,
    CapabilityValidationError,
    ConnectionRef,
    DriverResolutionError,
    HealthStatus,
)
from af.framework.relational import DatabaseStatement
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import EnvelopeKind, JsonEnvelopeCodec, MessageEnvelope, MessageMetadata
from af.queue import (
    DatabaseQueueDriver,
    QueueDispatchDisposition,
    QueueFeature,
    QueueJob,
    QueueManager,
    QueueReservationLost,
    SyncQueueDriver,
    UnsupportedQueueFeature,
    database_queue_registration,
)
from af.queue import database as database_module
from af.testing import ConformanceEvidence, FakeClock, QueueDriverConformanceSuite

_NOW = datetime(2026, 8, 1, 15, tzinfo=UTC)


def _job(message_id: str, **overrides: object) -> QueueJob:
    fields: dict[str, Any] = {
        "envelope": MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW,
            ),
            payload={"message_id": message_id, "secret": "queue-payload"},
        )
    }
    fields.update(overrides)
    return QueueJob(**fields)


def _token_factory(*tokens: str):  # type: ignore[no-untyped-def]
    remaining = iter(tokens)
    return lambda: next(remaining)


def test_sync_driver_rejects_unsupported_dispatch_features_directly() -> None:
    driver = SyncQueueDriver(lambda _job: None, clock=lambda: _NOW)

    with pytest.raises(UnsupportedQueueFeature) as captured:
        driver.dispatch(_job("job-delay", delay=timedelta(seconds=1)))

    assert captured.value.feature == QueueFeature.DELAY.value

    with pytest.raises(UnsupportedQueueFeature) as fifo:
        driver.dispatch(_job("job-fifo", fifo_group="orders"))
    assert fifo.value.feature == QueueFeature.FIFO.value

    with pytest.raises(UnsupportedQueueFeature) as transactional:
        driver.dispatch(_job("job-transaction", requires_transaction=True))
    assert transactional.value.feature == QueueFeature.TRANSACTIONAL_DISPATCH.value


def test_sync_driver_preserves_direct_dispatch_order() -> None:
    observed: list[str] = []
    driver = SyncQueueDriver(
        lambda job: observed.append(job.envelope.metadata.message_id),
        clock=lambda: _NOW,
    )

    driver.dispatch(_job("job-first"))
    driver.dispatch(_job("job-second"))

    assert observed == ["job-first", "job-second"]


def test_database_queue_passes_shared_conformance_suite(tmp_path: Path) -> None:
    database = tmp_path / "queue-conformance.sqlite3"

    @contextmanager
    def fixture() -> Iterator[DatabaseQueueDriver]:
        driver = DatabaseQueueDriver(SQLiteDriver(database), clock=FakeClock(_NOW))
        try:
            yield driver
        finally:
            driver.close()

    report = QueueDriverConformanceSuite().run(fixture)

    assert report.evidence is ConformanceEvidence.FAKE
    assert "enqueue-reserve-ack" in report.cases
    assert "retry-redelivery" in report.cases
    assert "retry-exhaustion" in report.cases


def test_database_queue_persists_jobs_across_driver_restart(tmp_path: Path) -> None:
    database = tmp_path / "queue-persist.sqlite3"
    first = DatabaseQueueDriver(SQLiteDriver(database), clock=FakeClock(_NOW))
    first.dispatch(_job("job-persist"))
    first.close()

    reopened = DatabaseQueueDriver(SQLiteDriver(database), clock=FakeClock(_NOW))
    try:
        reservation = reopened.reserve()

        assert reservation is not None
        assert reservation.job.envelope.metadata.message_id == "job-persist"
        reopened.ack(reservation)
        assert reopened.reserve() is None
    finally:
        reopened.close()


def test_database_queue_recovers_expired_reservation_with_new_fence(tmp_path: Path) -> None:
    database = tmp_path / "queue-lease-recovery.sqlite3"
    clock = FakeClock(_NOW)
    first = DatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=timedelta(seconds=30),
    )
    second = DatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=timedelta(seconds=30),
    )
    first.dispatch(_job("job-expired", max_attempts=2))

    original = first.reserve()
    assert original is not None
    assert original.attempt == 1
    assert original.expires_at == _NOW + timedelta(seconds=30)
    assert second.reserve() is None

    clock.advance(timedelta(seconds=30))
    replacement = second.reserve()

    assert replacement is not None
    assert replacement.attempt == 2
    assert replacement.fencing_token == 2
    assert replacement.expires_at == _NOW + timedelta(seconds=60)
    with pytest.raises(QueueReservationLost) as stale:
        first.ack(original)
    assert stale.value.stage == "queue.ack"
    second.ack(replacement)
    first.close()
    second.close()


@pytest.mark.parametrize("operation", ["ack", "reject", "release", "retry"])
def test_database_queue_foreign_driver_cannot_settle_owned_reservation(
    tmp_path: Path,
    operation: str,
) -> None:
    database = tmp_path / f"queue-owner-{operation}.sqlite3"
    clock = FakeClock(_NOW)
    owner = DatabaseQueueDriver(SQLiteDriver(database), clock=clock)
    foreign = DatabaseQueueDriver(SQLiteDriver(database), clock=clock)
    owner.dispatch(_job(f"job-owner-{operation}", max_attempts=2))
    reservation = owner.reserve()
    assert reservation is not None

    with pytest.raises(QueueReservationLost) as lost:
        getattr(foreign, operation)(reservation)

    assert lost.value.stage == f"queue.{operation}"
    owner.ack(reservation)
    owner.close()
    foreign.close()


def test_database_queue_acquisition_identity_fences_delete_redispatch_aba(
    tmp_path: Path,
) -> None:
    token = _token_factory("a" * 64, "b" * 64, "c" * 64)
    driver = DatabaseQueueDriver(
        SQLiteDriver(tmp_path / "queue-owner-aba.sqlite3"),
        clock=FakeClock(_NOW),
        token_factory=token,
    )
    job = _job("job-owner-aba")
    driver.dispatch(job)
    original = driver.reserve()
    assert original is not None
    driver.ack(original)
    driver.dispatch(job)
    replacement = driver.reserve()
    assert replacement is not None

    assert replacement.fencing_token == original.fencing_token == 1
    assert replacement.reserved_at == original.reserved_at
    assert replacement.owner_id == original.owner_id == "a" * 64
    assert replacement.reservation_id != original.reservation_id
    with pytest.raises(QueueReservationLost):
        driver.ack(original)
    driver.ack(replacement)
    driver.close()


def test_database_queue_identical_rows_are_bound_to_their_database_driver(tmp_path: Path) -> None:
    clock = FakeClock(_NOW)
    first = DatabaseQueueDriver(SQLiteDriver(tmp_path / "queue-owner-a.sqlite3"), clock=clock)
    second = DatabaseQueueDriver(SQLiteDriver(tmp_path / "queue-owner-b.sqlite3"), clock=clock)
    job = _job("job-owner-cross-database")
    first.dispatch(job)
    second.dispatch(job)
    first_reservation = first.reserve()
    second_reservation = second.reserve()
    assert first_reservation is not None
    assert second_reservation is not None
    assert first_reservation.fencing_token == second_reservation.fencing_token
    assert first_reservation.reserved_at == second_reservation.reserved_at

    with pytest.raises(QueueReservationLost):
        second.ack(first_reservation)

    second.ack(second_reservation)
    first.ack(first_reservation)
    first.close()
    second.close()


def test_database_queue_rejects_forged_acquisition_identity(tmp_path: Path) -> None:
    driver = DatabaseQueueDriver(
        SQLiteDriver(tmp_path / "queue-owner-forged.sqlite3"),
        clock=FakeClock(_NOW),
    )
    driver.dispatch(_job("job-owner-forged"))
    reservation = driver.reserve()
    assert reservation is not None
    forged = replace(reservation, reservation_id="f" * 64)

    with pytest.raises(QueueReservationLost):
        driver.ack(forged)

    driver.ack(reservation)
    driver.close()


def test_database_queue_rejects_use_after_process_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = DatabaseQueueDriver(
        SQLiteDriver(tmp_path / "queue-owner-fork.sqlite3"),
        clock=FakeClock(_NOW),
    )
    driver.dispatch(_job("job-owner-fork"))
    reservation = driver.reserve()
    assert reservation is not None
    original_pid = database_module.os.getpid()
    monkeypatch.setattr(database_module.os, "getpid", lambda: original_pid + 1)

    with pytest.raises(Exception) as inherited:
        driver.ack(reservation)
    assert getattr(inherited.value, "reason", None) is PlatformReason.INTEGRITY_FAILURE
    assert getattr(inherited.value, "stage", None) == "queue.connection"

    monkeypatch.setattr(database_module.os, "getpid", lambda: original_pid)
    driver.ack(reservation)
    driver.close()


def test_queue_manager_binds_settlement_to_reserving_connection(tmp_path: Path) -> None:
    database = tmp_path / "queue-owner-manager.sqlite3"
    manager = QueueManager(
        connections={
            "primary": {"driver": "database"},
            "secondary": {"driver": "database"},
        },
        default="secondary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=FakeClock(_NOW),
    )
    manager.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(database),
            clock=FakeClock(_NOW),
        )
    )
    manager.dispatch(_job("job-owner-manager"), name="primary")
    reservation = manager.reserve(name="primary")
    assert reservation is not None
    assert reservation.connection_name == "primary"

    with pytest.raises(QueueReservationLost):
        manager.ack(reservation, name="secondary")

    manager.ack(reservation)
    manager.close()


def test_queue_manager_rejects_forged_route_binding_before_connection_resolution(
    tmp_path: Path,
) -> None:
    manager = QueueManager(
        connections={"primary": {"driver": "database"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=FakeClock(_NOW),
    )
    manager.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(tmp_path / "queue-owner-route.sqlite3"),
            clock=FakeClock(_NOW),
        )
    )
    manager.dispatch(_job("job-owner-route"))
    reservation = manager.reserve()
    assert reservation is not None
    forged = replace(
        reservation,
        connection_name="unconfigured",
        connection_binding="f" * 64,
    )

    with pytest.raises(QueueReservationLost):
        manager.ack(forged)

    manager.ack(reservation)
    manager.close()


def test_queue_manager_rejects_removal_of_owned_route_provenance(tmp_path: Path) -> None:
    manager = QueueManager(
        connections={"primary": {"driver": "database"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=FakeClock(_NOW),
    )
    manager.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(tmp_path / "queue-owner-downgrade.sqlite3"),
            clock=FakeClock(_NOW),
        )
    )
    manager.dispatch(_job("job-owner-downgrade"))
    reservation = manager.reserve()
    assert reservation is not None
    downgraded = replace(reservation, connection_name=None, connection_binding=None)

    with pytest.raises(QueueReservationLost):
        manager.ack(downgraded)

    manager.ack(reservation)
    manager.close()


def test_database_queue_token_failure_does_not_claim_job(tmp_path: Path) -> None:
    calls = 0

    def token() -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("entropy unavailable")
        return f"{calls:064x}"

    driver = DatabaseQueueDriver(
        SQLiteDriver(tmp_path / "queue-owner-token-failure.sqlite3"),
        clock=FakeClock(_NOW),
        token_factory=token,
    )
    driver.dispatch(_job("job-owner-token-failure"))

    with pytest.raises(Exception) as failed:
        driver.reserve()
    assert getattr(failed.value, "reason", None) is PlatformReason.INTEGRITY_FAILURE

    reservation = driver.reserve()
    assert reservation is not None
    assert reservation.fencing_token == 1
    assert reservation.attempt == 1
    driver.ack(reservation)
    driver.close()


@pytest.mark.parametrize("operation", ["ack", "reject", "release", "retry"])
def test_database_queue_fences_every_stale_settlement_after_lease_takeover(
    tmp_path: Path,
    operation: str,
) -> None:
    database = tmp_path / f"queue-stale-{operation}.sqlite3"
    clock = FakeClock(_NOW)
    first = DatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=timedelta(seconds=5),
    )
    second = DatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=timedelta(seconds=5),
    )
    first.dispatch(_job(f"job-stale-{operation}", max_attempts=3))
    original = first.reserve()
    assert original is not None
    clock.advance(timedelta(seconds=5))
    replacement = second.reserve()
    assert replacement is not None

    with pytest.raises(QueueReservationLost) as stale:
        getattr(first, operation)(original)

    assert stale.value.stage == f"queue.{operation}"
    second.ack(replacement)
    first.close()
    second.close()


def test_database_queue_dead_letters_expired_final_attempt(tmp_path: Path) -> None:
    database = tmp_path / "queue-expired-final.sqlite3"
    clock = FakeClock(_NOW)
    first = DatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=timedelta(seconds=10),
    )
    first.dispatch(_job("job-expired-final", max_attempts=1))
    assert first.reserve() is not None
    first.close()

    clock.advance(timedelta(seconds=10))
    reopened = DatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=timedelta(seconds=10),
    )
    assert reopened.reserve() is None
    assert reopened.reserve() is None
    reopened.close()


@pytest.mark.parametrize("operation", ["ack", "reject", "release", "retry"])
def test_database_queue_fences_settlement_at_exact_expiry_before_takeover(
    tmp_path: Path,
    operation: str,
) -> None:
    clock = FakeClock(_NOW)
    driver = DatabaseQueueDriver(
        SQLiteDriver(tmp_path / f"queue-expiry-{operation}.sqlite3"),
        clock=clock,
        reservation_lease=timedelta(seconds=5),
    )
    driver.dispatch(_job(f"job-expiry-{operation}", max_attempts=2))
    reservation = driver.reserve()
    assert reservation is not None

    clock.advance(timedelta(seconds=5))
    with pytest.raises(QueueReservationLost):
        getattr(driver, operation)(reservation)

    replacement = driver.reserve()
    assert replacement is not None
    assert replacement.attempt == 2
    driver.ack(replacement)
    driver.close()


def test_database_queue_recovers_active_legacy_reservation_at_implicit_expiry(
    tmp_path: Path,
) -> None:
    database = SQLiteDriver(tmp_path / "queue-legacy-active.sqlite3")
    job = _job("job-legacy-active", max_attempts=2)
    encoded = JsonEnvelopeCodec().encode(job.envelope).decode("utf-8")
    timestamp = _NOW.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with database.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                """CREATE TABLE af_queue_jobs (
                    message_id VARCHAR(200) PRIMARY KEY,
                    message_type VARCHAR(128) NOT NULL,
                    schema_version INTEGER NOT NULL,
                    occurred_at VARCHAR(32) NOT NULL,
                    lane VARCHAR(128) NOT NULL,
                    available_at VARCHAR(32) NOT NULL,
                    envelope_text TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    reservation_generation INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    backoff_seconds REAL NULL,
                    reserved_at VARCHAR(32) NULL,
                    dead_lettered_at VARCHAR(32) NULL
                )"""
            )
        )
        transaction.execute(
            DatabaseStatement(
                """INSERT INTO af_queue_jobs (
                    message_id, message_type, schema_version, occurred_at, lane,
                    available_at, envelope_text, attempt_count, reservation_generation,
                    max_attempts, backoff_seconds, reserved_at, dead_lettered_at
                ) VALUES (
                    :message_id, 'jobs.example', 1, :occurred_at, 'default',
                    :available_at, :envelope_text, 0, 1, 2, NULL, :reserved_at, NULL
                )""",
                {
                    "message_id": "job-legacy-active",
                    "occurred_at": timestamp,
                    "available_at": timestamp,
                    "envelope_text": encoded,
                    "reserved_at": timestamp,
                },
            )
        )

    clock = FakeClock(_NOW + timedelta(seconds=4))
    driver = DatabaseQueueDriver(
        database,
        clock=clock,
        reservation_lease=timedelta(seconds=5),
    )
    assert driver.reserve() is None
    clock.advance(timedelta(seconds=1))
    replacement = driver.reserve()
    assert replacement is not None
    assert replacement.attempt == 2
    assert replacement.fencing_token == 2
    assert replacement.owner_id is not None
    assert replacement.reservation_id is not None
    driver.ack(replacement)
    driver.close()


@pytest.mark.parametrize("expired", [False, True])
def test_database_queue_serializes_concurrent_sqlite_claims(
    tmp_path: Path,
    expired: bool,
) -> None:
    database = tmp_path / f"queue-concurrent-{expired}.sqlite3"
    clock = FakeClock(_NOW)
    setup = DatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=timedelta(seconds=5),
    )
    setup.dispatch(_job(f"job-concurrent-{str(expired).lower()}", max_attempts=3))
    if expired:
        assert setup.reserve() is not None
        clock.advance(timedelta(seconds=5))
    setup.close()

    drivers = [
        DatabaseQueueDriver(
            SQLiteDriver(database),
            clock=clock,
            reservation_lease=timedelta(seconds=5),
        )
        for _ in range(2)
    ]
    start = Barrier(3)
    lock = Lock()
    reservations: list[object] = []
    errors: list[BaseException] = []

    def claim(driver: DatabaseQueueDriver) -> None:
        start.wait()
        try:
            result = driver.reserve()
            if result is not None:
                with lock:
                    reservations.append(result)
        except BaseException as error:
            with lock:
                errors.append(error)

    threads = [Thread(target=claim, args=(driver,)) for driver in drivers]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert errors == []
    assert len(reservations) == 1
    for driver in drivers:
        driver.close()


def test_database_queue_rejects_invalid_reservation_lease(tmp_path: Path) -> None:
    for lease in (timedelta(0), timedelta(seconds=-1), timedelta(days=2)):
        with pytest.raises(ValueError, match="reservation lease"):
            DatabaseQueueDriver(
                SQLiteDriver(tmp_path / f"invalid-{lease.total_seconds()}.sqlite3"),
                clock=FakeClock(_NOW),
                reservation_lease=lease,
            )


def test_database_queue_registration_binds_runtime_identity_and_evidence(tmp_path: Path) -> None:
    observed_at = datetime(2026, 8, 1, 16, tzinfo=UTC)
    database = tmp_path / "queue-registration.sqlite3"
    registration = database_queue_registration(
        database_factory=lambda: SQLiteDriver(database),
        clock=lambda: observed_at,
    )
    ref = ConnectionRef("queue", "primary", "database")
    driver = registration.factory(ref, {"driver": "database"})
    binding = CapabilityBinding(
        driver_version=registration.manifest.version,
        artifact_digest=registration.manifest.artifact_digest,
        dependency_graph_digest=registration.manifest.dependency_graph_digest,
        configuration_digest="a" * 64,
        deployment_profile="test",
        policy_version="policy-v1",
    )

    try:
        evidence = registration.verifier(ref, driver, binding)
        assert evidence.binding == binding
        assert evidence.health is HealthStatus.HEALTHY
        assert evidence.observed_at == observed_at
        assert evidence.capabilities == CapabilitySet(
            (
                QueueFeature.RESERVATION.value,
                QueueFeature.RETRY.value,
            )
        )
    finally:
        disposer = registration.disposer
        if disposer is not None:
            disposer(driver)
        else:
            driver.close()


def test_database_queue_registration_rejects_invalid_inputs_and_configuration() -> None:
    with pytest.raises(TypeError, match="factory"):
        database_queue_registration(database_factory="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="clock"):
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(":memory:"),
            clock="not callable",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="reservation lease"):
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(":memory:"),
            reservation_lease=timedelta(0),
        )

    manager = QueueManager(
        connections={"primary": {"driver": "database", "extra": True}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(database_queue_registration(database_factory=lambda: SQLiteDriver(":memory:")))

    with pytest.raises(DriverResolutionError):
        manager.connection()
    manager.close()


def test_database_queue_registration_rejects_invalid_runtime_evidence_time() -> None:
    manager = QueueManager(
        connections={"primary": {"driver": "database"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(":memory:"),
            clock=lambda: datetime(2026, 8, 1, 16),
        )
    )

    with pytest.raises(CapabilityValidationError):
        manager.connection()
    manager.close()


def test_queue_manager_routes_database_driver_retry_and_dead_letter(tmp_path: Path) -> None:
    manager = QueueManager(
        connections={"primary": {"driver": "database"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=FakeClock(_NOW),
    )
    manager.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(tmp_path / "queue-manager.sqlite3"),
            clock=FakeClock(_NOW),
        )
    )

    receipt = manager.dispatch(_job("job-retry", max_attempts=2, backoff=timedelta(seconds=5)))
    assert receipt.disposition is QueueDispatchDisposition.ENQUEUED

    first = manager.reserve()
    assert first is not None
    assert first.attempt == 1
    manager.retry(first, available_at=first.reserved_at)

    second = manager.reserve()
    assert second is not None
    assert second.attempt == 2
    manager.retry(second, available_at=second.reserved_at)

    assert manager.reserve() is None
    manager.close()


def test_queue_manager_releases_cancelled_reservation_without_consuming_attempt(
    tmp_path: Path,
) -> None:
    manager = QueueManager(
        connections={"primary": {"driver": "database"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=FakeClock(_NOW),
    )
    manager.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(tmp_path / "queue-release.sqlite3"),
            clock=FakeClock(_NOW),
        )
    )
    manager.dispatch(_job("job-release"))

    first = manager.reserve()
    assert first is not None
    assert first.attempt == 1
    manager.release(first)

    second = manager.reserve()
    assert second is not None
    assert second.attempt == 1
    with pytest.raises(Exception) as stale:
        manager.ack(first)
    assert getattr(stale.value, "reason", None) is PlatformReason.INTEGRITY_FAILURE
    manager.ack(second)
    manager.close()


def test_database_queue_upgrades_legacy_table_without_losing_queued_work(
    tmp_path: Path,
) -> None:
    database = SQLiteDriver(tmp_path / "queue-legacy.sqlite3")
    job = _job("job-before-upgrade")
    encoded = JsonEnvelopeCodec().encode(job.envelope).decode("utf-8")
    timestamp = _NOW.isoformat(timespec="microseconds").replace("+00:00", "Z")
    with database.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                """CREATE TABLE af_queue_jobs (
                    message_id VARCHAR(200) PRIMARY KEY,
                    message_type VARCHAR(128) NOT NULL,
                    schema_version INTEGER NOT NULL,
                    occurred_at VARCHAR(32) NOT NULL,
                    lane VARCHAR(128) NOT NULL,
                    available_at VARCHAR(32) NOT NULL,
                    envelope_text TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    backoff_seconds REAL NULL,
                    reserved_at VARCHAR(32) NULL,
                    dead_lettered_at VARCHAR(32) NULL
                )"""
            )
        )
        transaction.execute(
            DatabaseStatement(
                """INSERT INTO af_queue_jobs (
                    message_id, message_type, schema_version, occurred_at, lane,
                    available_at, envelope_text, attempt_count, max_attempts,
                    backoff_seconds, reserved_at, dead_lettered_at
                ) VALUES (
                    :message_id, :message_type, 1, :occurred_at, 'default',
                    :available_at, :envelope_text, 0, 1, NULL, NULL, NULL
                )""",
                {
                    "message_id": "job-before-upgrade",
                    "message_type": "jobs.example",
                    "occurred_at": timestamp,
                    "available_at": timestamp,
                    "envelope_text": encoded,
                },
            )
        )

    driver = DatabaseQueueDriver(database, clock=FakeClock(_NOW))
    reservation = driver.reserve()

    assert reservation is not None
    assert reservation.job.envelope.metadata.message_id == "job-before-upgrade"
    assert reservation.fencing_token == 1
    assert reservation.owner_id is not None
    assert reservation.reservation_id is not None
    driver.ack(reservation)
    driver.close()


def test_database_queue_driver_reports_terminal_rejections_and_shutdown(tmp_path: Path) -> None:
    driver = DatabaseQueueDriver(
        SQLiteDriver(tmp_path / "queue-terminal.sqlite3"),
        clock=FakeClock(_NOW),
    )
    driver.dispatch(_job("job-terminal"))
    reservation = driver.reserve()

    assert reservation is not None
    driver.reject(reservation)
    assert driver.reserve() is None

    driver.close()
    driver.close()

    with pytest.raises(Exception) as captured:
        driver.dispatch(_job("job-after-close"))
    assert getattr(captured.value, "reason", None) is PlatformReason.SHUTDOWN_FAILED


def test_database_queue_rejects_unsupported_fifo_and_transaction_requests(tmp_path: Path) -> None:
    driver = DatabaseQueueDriver(
        SQLiteDriver(tmp_path / "queue-unsupported.sqlite3"),
        clock=FakeClock(_NOW),
    )

    try:
        with pytest.raises(UnsupportedQueueFeature) as fifo:
            driver.dispatch(_job("job-fifo", fifo_group="orders"))
        assert fifo.value.feature == QueueFeature.FIFO.value

        with pytest.raises(UnsupportedQueueFeature) as transactional:
            driver.dispatch(_job("job-transaction", requires_transaction=True))
        assert transactional.value.feature == QueueFeature.TRANSACTIONAL_DISPATCH.value
    finally:
        driver.close()


def test_database_queue_reserves_jobs_in_available_then_occurred_then_id_order(
    tmp_path: Path,
) -> None:
    base = datetime(2026, 8, 1, 15, tzinfo=UTC)
    driver = DatabaseQueueDriver(
        SQLiteDriver(tmp_path / "queue-ordering.sqlite3"),
        clock=FakeClock(base + timedelta(minutes=5)),
    )

    first = _job(
        "job-b",
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id="job-b",
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=base + timedelta(seconds=2),
            ),
            payload={"message_id": "job-b", "secret": "queue-payload"},
        ),
    )
    second = _job(
        "job-a",
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id="job-a",
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=base + timedelta(seconds=2),
            ),
            payload={"message_id": "job-a", "secret": "queue-payload"},
        ),
    )
    third = _job(
        "job-earliest",
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id="job-earliest",
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=base + timedelta(seconds=1),
            ),
            payload={"message_id": "job-earliest", "secret": "queue-payload"},
        ),
    )

    try:
        driver.dispatch(first)
        driver.dispatch(second)
        driver.dispatch(third)

        reservation_one = driver.reserve()
        reservation_two = driver.reserve()
        reservation_three = driver.reserve()

        assert reservation_one is not None
        assert reservation_two is not None
        assert reservation_three is not None
        assert reservation_one.job.envelope.metadata.message_id == "job-earliest"
        assert reservation_two.job.envelope.metadata.message_id == "job-a"
        assert reservation_three.job.envelope.metadata.message_id == "job-b"
    finally:
        driver.close()
