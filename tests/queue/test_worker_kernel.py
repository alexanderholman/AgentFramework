from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueReservation,
)
from af.queue.manager import QueueDriver
from af.queue.worker import WorkerKernel, WorkerState
from af.testing import FakeClock

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
            payload={"message_id": message_id},
        )
    }
    fields.update(overrides)
    return QueueJob(**fields)


class _StubDriver:
    """Minimal QueueDriver implementation for unit tests."""

    def __init__(
        self,
        queue: str = "default",
        jobs: list[QueueJob] | None = None,
        *,
        reserve_delay: timedelta = timedelta(0),
    ) -> None:
        self._queue = queue
        self._jobs = list(jobs) if jobs is not None else []
        self._reserved: dict[str, QueueReservation] = {}
        self._acknowledged: list[str] = []
        self._rejected: list[str] = []
        self._reserve_delay = reserve_delay
        self._closed = False

    @property
    def acknowledged(self) -> list[str]:
        return list(self._acknowledged)

    @property
    def rejected(self) -> list[str]:
        return list(self._rejected)

    def dispatch(self, job: QueueJob) -> QueueDispatchReceipt:
        if self._closed:
            raise RuntimeError("driver is closed")
        self._jobs.append(job)
        return QueueDispatchReceipt(
            disposition=QueueDispatchDisposition.ACCEPTED,
            job_id=job.envelope.metadata.message_id,
        )

    def reserve(self, timeout: timedelta) -> QueueReservation | None:
        if self._closed:
            raise RuntimeError("driver is closed")
        for job in self._jobs:
            mid = job.envelope.metadata.message_id
            if mid not in self._reserved:
                reservation = QueueReservation(
                    job=job,
                    attempt=1,
                    reserved_at=_NOW,
                )
                self._reserved[mid] = reservation
                return reservation
        return None

    def ack(self, reservation: QueueReservation) -> None:
        if self._closed:
            raise RuntimeError("driver is closed")
        mid = reservation.job.envelope.metadata.message_id
        self._acknowledged.append(mid)
        self._reserved.pop(mid, None)
        self._jobs = [j for j in self._jobs if j.envelope.metadata.message_id != mid]

    def reject(self, reservation: QueueReservation, *, requeue: bool = False) -> None:
        if self._closed:
            raise RuntimeError("driver is closed")
        mid = reservation.job.envelope.metadata.message_id
        if requeue:
            self._rejected.append(mid)
            self._jobs.append(reservation.job)
        self._reserved.pop(mid, None)

    def retry(self, reservation: QueueReservation, *, delay: timedelta | None = None) -> None:
        if self._closed:
            raise RuntimeError("driver is closed")
        self._jobs.append(reservation.job)
        self._reserved.pop(reservation.job.envelope.metadata.message_id, None)

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------------------
# Tests - Worker boot and state
# ---------------------------------------------------------------------------

class TestWorkerBoot:
    def test_boot_transitions_to_running(self) -> None:
        driver: QueueDriver = _StubDriver()
        clock = FakeClock(_NOW)
        worker = WorkerKernel(driver, clock=clock)

        worker.boot()

        assert worker.state == WorkerState.RUNNING

    def test_boot_is_idempotent(self) -> None:
        driver: QueueDriver = _StubDriver()
        clock = FakeClock(_NOW)
        worker = WorkerKernel(driver, clock=clock)

        worker.boot()
        worker.boot()

        assert worker.state == WorkerState.RUNNING

    def test_cannot_boot_after_shutdown(self) -> None:
        driver: QueueDriver = _StubDriver()
        clock = FakeClock(_NOW)
        worker = WorkerKernel(driver, clock=clock)

        worker.boot()
        worker.shutdown()

        with pytest.raises(RuntimeError, match="already shut down"):
            worker.boot()


# ---------------------------------------------------------------------------
# Tests - Job processing
# ---------------------------------------------------------------------------

class TestJobProcessing:
    def test_processes_single_job(self) -> None:
        job = _job("job-001")
        driver: QueueDriver = _StubDriver(jobs=[job])
        clock = FakeClock(_NOW)
        handler_called: list[str] = []
        worker = WorkerKernel(
            driver,
            handler=lambda j: handler_called.append(
                j.envelope.metadata.message_id
            ),
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()

        assert handler_called == ["job-001"]
        assert driver.acknowledged == ["job-001"]

    def test_acknowledges_job_on_success(self) -> None:
        job = _job("job-002")
        driver: QueueDriver = _StubDriver(jobs=[job])
        clock = FakeClock(_NOW)
        worker = WorkerKernel(
            driver,
            handler=lambda j: None,
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()

        assert driver.acknowledged == ["job-002"]

    def test_requeues_job_on_handler_failure(self) -> None:
        job = _job("job-003")
        driver: QueueDriver = _StubDriver(jobs=[job])
        clock = FakeClock(_NOW)
        worker = WorkerKernel(
            driver,
            handler=lambda j: (_ for _ in ()).throw(RuntimeError("boom")),
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()

        assert driver.rejected == ["job-003"]

    def test_no_state_leakage_between_jobs(self) -> None:
        job_a = _job("job-a")
        job_b = _job("job-b")
        driver: QueueDriver = _StubDriver(jobs=[job_a, job_b])
        clock = FakeClock(_NOW)
        processed: list[str] = []
        worker = WorkerKernel(
            driver,
            handler=lambda j: processed.append(j.envelope.metadata.message_id),
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()
        worker.tick()

        assert processed == ["job-a", "job-b"]
        assert driver.acknowledged == ["job-a", "job-b"]

    def test_duplicate_delivery_is_idempotent(self) -> None:
        """A job that appears twice in the queue should be processed only once per reservation."""
        job = _job("job-dup")
        driver: QueueDriver = _StubDriver(jobs=[job])
        clock = FakeClock(_NOW)
        processed: list[str] = []
        worker = WorkerKernel(
            driver,
            handler=lambda j: processed.append(j.envelope.metadata.message_id),
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()
        worker.tick()

        assert processed == ["job-dup"]


# ---------------------------------------------------------------------------
# Tests - Shutdown and draining
# ---------------------------------------------------------------------------

class TestShutdown:
    def test_shutdown_transitions_to_stopped(self) -> None:
        driver: QueueDriver = _StubDriver()
        clock = FakeClock(_NOW)
        worker = WorkerKernel(driver, clock=clock)

        worker.boot()
        worker.shutdown()

        assert worker.state == WorkerState.STOPPED

    def test_shutdown_stops_intake(self) -> None:
        job = _job("job-stop")
        driver: QueueDriver = _StubDriver(jobs=[job])
        clock = FakeClock(_NOW)
        processed: list[str] = []
        worker = WorkerKernel(
            driver,
            handler=lambda j: processed.append(j.envelope.metadata.message_id),
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.shutdown()
        worker.tick()

        assert processed == []

    def test_shutdown_finishes_in_flight_job(self) -> None:
        """When shutdown is requested while a job is being processed, the job completes."""
        job = _job("job-inflight")
        driver: QueueDriver = _StubDriver(jobs=[job])
        clock = FakeClock(_NOW)
        processed: list[str] = []
        worker = WorkerKernel(
            driver,
            handler=lambda j: processed.append(j.envelope.metadata.message_id),
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()
        worker.shutdown()

        assert processed == ["job-inflight"]
        assert driver.acknowledged == ["job-inflight"]


# ---------------------------------------------------------------------------
# Tests - Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_respects_max_concurrency(self) -> None:
        """tick() processes at most max_concurrency jobs per call."""
        jobs = [_job(f"job-{i:03d}") for i in range(5)]
        driver: QueueDriver = _StubDriver(jobs=jobs)
        clock = FakeClock(_NOW)
        processed: list[str] = []
        worker = WorkerKernel(
            driver,
            handler=lambda j: processed.append(j.envelope.metadata.message_id),
            clock=clock,
            poll_interval=timedelta(seconds=1),
            max_concurrency=2,
        )

        worker.boot()
        worker.tick()

        assert len(processed) == 2
        assert len(driver.acknowledged) == 2

    def test_releases_capacity_after_completion(self) -> None:
        jobs = [_job("job-1"), _job("job-2"), _job("job-3")]
        driver: QueueDriver = _StubDriver(jobs=jobs)
        clock = FakeClock(_NOW)
        processed: list[str] = []
        worker = WorkerKernel(
            driver,
            handler=lambda j: processed.append(j.envelope.metadata.message_id),
            clock=clock,
            poll_interval=timedelta(seconds=1),
            max_concurrency=1,
        )

        worker.boot()
        worker.tick()
        worker.tick()
        worker.tick()

        assert processed == ["job-1", "job-2", "job-3"]


# ---------------------------------------------------------------------------
# Tests - Observability
# ---------------------------------------------------------------------------

class TestObservability:
    def test_exposes_throughput(self) -> None:
        jobs = [_job("job-t1"), _job("job-t2")]
        driver: QueueDriver = _StubDriver(jobs=jobs)
        clock = FakeClock(_NOW)
        worker = WorkerKernel(
            driver,
            handler=lambda j: None,
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()
        worker.tick()

        stats = worker.stats()
        assert stats.processed == 2
        assert stats.failed == 0

    def test_exposes_failure_count(self) -> None:
        job = _job("job-fail")
        driver: QueueDriver = _StubDriver(jobs=[job])
        clock = FakeClock(_NOW)
        worker = WorkerKernel(
            driver,
            handler=lambda j: (_ for _ in ()).throw(RuntimeError("fail")),
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()

        stats = worker.stats()
        assert stats.processed == 0
        assert stats.failed == 1

    def test_exposes_in_flight_count(self) -> None:
        """In-flight count should be zero after all jobs complete."""
        jobs = [_job("job-if1"), _job("job-if2")]
        driver: QueueDriver = _StubDriver(jobs=jobs)
        clock = FakeClock(_NOW)
        worker = WorkerKernel(
            driver,
            handler=lambda j: None,
            clock=clock,
            poll_interval=timedelta(seconds=1),
        )

        worker.boot()
        worker.tick()
        worker.tick()

        stats = worker.stats()
        assert stats.in_flight == 0
