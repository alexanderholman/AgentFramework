from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from typing import cast

from af.framework.relational import DatabaseDriver
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import (
    DatabaseQueueDriver,
    QueueJob,
    QueueReservation,
    QueueReservationLost,
    WorkerKernel,
)
from af.queue.worker import JobHandler

_NOW = datetime(2026, 8, 17, 22, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, duration: timedelta) -> None:
        self.now += duration


class _PausedRenewalDatabaseQueueDriver(DatabaseQueueDriver):
    """Model a worker process paused after its heartbeat interval elapsed."""

    def __init__(
        self,
        database: DatabaseDriver,
        *,
        clock: Callable[[], datetime],
        reservation_lease: timedelta,
    ) -> None:
        super().__init__(
            database,
            clock=clock,
            reservation_lease=reservation_lease,
        )
        self.renewal_started = Event()
        self.resume_renewal = Event()

    def renew(self, reservation: QueueReservation, /) -> QueueReservation:
        self.renewal_started.set()
        if not self.resume_renewal.wait(timeout=2):
            raise TimeoutError("test did not resume stale worker renewal")
        return super().renew(reservation)


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
        ),
        max_attempts=3,
    )


def test_replacement_worker_fences_paused_predecessor_renewal_and_settlement(
    tmp_path: Path,
) -> None:
    database = tmp_path / "queue-worker-stale-heartbeat.sqlite3"
    clock = _Clock()
    lease = timedelta(milliseconds=100)
    old_driver = _PausedRenewalDatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=lease,
    )
    replacement_driver = DatabaseQueueDriver(
        SQLiteDriver(database),
        clock=clock,
        reservation_lease=lease,
    )
    old_driver.dispatch(_job("stale-worker-delivery"))
    old_kernel = WorkerKernel(
        old_driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=10),
        clock=clock,
    )
    replacement_kernel = WorkerKernel(
        replacement_driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=10),
        clock=clock,
    )
    old_handler_started = Event()
    finish_old_handler = Event()
    old_failures: list[BaseException] = []

    def handle_old_job(_job: QueueJob) -> None:
        old_handler_started.set()
        assert finish_old_handler.wait(timeout=2)

    def run_old_worker() -> None:
        try:
            old_kernel.run_once(cast(JobHandler, handle_old_job))
        except BaseException as error:
            old_failures.append(error)

    old_thread = Thread(target=run_old_worker)
    old_thread.start()
    try:
        assert old_handler_started.wait(timeout=2)
        assert old_driver.renewal_started.wait(timeout=2)

        clock.advance(lease)
        replacement = replacement_kernel.run_once(lambda _job: None)
        assert replacement is not None
        assert replacement.fencing_token == 2

        old_driver.resume_renewal.set()
        finish_old_handler.set()
        old_thread.join(timeout=2)

        assert not old_thread.is_alive()
        assert len(old_failures) == 1
        assert isinstance(old_failures[0], QueueReservationLost)
        assert old_failures[0].stage == "queue.renew"
        assert replacement_driver.reserve() is None
    finally:
        old_driver.resume_renewal.set()
        finish_old_handler.set()
        old_thread.join(timeout=2)
        old_kernel.close()
        replacement_kernel.close()
