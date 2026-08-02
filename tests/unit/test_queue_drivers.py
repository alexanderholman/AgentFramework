from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import (
    DatabaseQueueDriver,
    QueueDispatchDisposition,
    QueueFeature,
    QueueJob,
    QueueManager,
    SyncQueueDriver,
    UnsupportedQueueFeature,
    database_queue_registration,
)
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
