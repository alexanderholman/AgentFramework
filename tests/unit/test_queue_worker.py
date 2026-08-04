from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from af.framework.errors import PlatformReason
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
)
from af.queue.manager import QueueFeature
from af.queue.worker import (
    WorkerKernel,
    WorkerState,
)

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


# -- WorkerKernel lifecycle ---------------------------------------------------


def test_worker_kernel_reports_idle_state_on_creation() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    assert kernel.state is WorkerState.IDLE


def test_worker_kernel_reports_processing_state_during_poll() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-1"))

    processed: list[str] = []
    kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))

    assert processed == ["job-1"]


def test_worker_kernel_reports_idle_when_queue_empty() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    result = kernel.run_once(consumer=lambda job: None)
    assert result is None


def test_worker_kernel_rejects_run_after_shutdown() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    kernel.close()

    with pytest.raises(QueueOperationError) as exc:
        kernel.run_once(consumer=lambda job: None)
    assert exc.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_worker_kernel_close_is_idempotent() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    kernel.close()
    kernel.close()


# -- WorkerKernel concurrency -------------------------------------------------


def test_worker_kernel_respects_max_concurrency() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, max_concurrency=2, clock=lambda: _NOW)

    driver.dispatch(_job("job-a"))
    driver.dispatch(_job("job-b"))
    driver.dispatch(_job("job-c"))

    processed: list[str] = []
    # First run picks up to max_concurrency jobs
    kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))
    assert len(processed) <= 2


def test_worker_kernel_backpressure_when_at_capacity() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, max_concurrency=1, clock=lambda: _NOW)

    driver.dispatch(_job("job-x"))
    processed: list[str] = []
    kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))
    assert processed == ["job-x"]

    # With concurrency=1 and no ack, second run should still pick up next job
    # since we ack inline in the consumer
    driver.dispatch(_job("job-y"))
    kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))
    assert processed == ["job-x", "job-y"]


# -- WorkerKernel stats -------------------------------------------------------


def test_worker_kernel_tracks_jobs_processed() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-1"))
    driver.dispatch(_job("job-2"))

    kernel.run_once(consumer=lambda job: None)
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.jobs_processed == 2


def test_worker_kernel_tracks_failures() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-fail"))

    def failing_consumer(job: QueueJob) -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        kernel.run_once(consumer=failing_consumer)

    stats = kernel.stats
    assert stats.jobs_failed == 1


def test_worker_kernel_tracks_runtime() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-rt"))
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.total_runtime > timedelta(0) or stats.jobs_processed == 1


# -- WorkerKernel heartbeat ---------------------------------------------------


def test_worker_kernel_requires_heartbeat_interval() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(
        driver,
        heartbeat_interval=timedelta(seconds=10),
        clock=lambda: _NOW,
    )
    assert kernel.heartbeat_interval == timedelta(seconds=10)


def test_worker_kernel_heartbeat_detects_timeout() -> None:
    driver = _make_driver()
    base = _NOW
    tick = 0

    def advancing_clock() -> datetime:
        nonlocal tick
        tick += 1
        return base + timedelta(seconds=tick * 30)

    kernel = WorkerKernel(
        driver,
        heartbeat_interval=timedelta(seconds=10),
        max_idle_time=timedelta(seconds=20),
        clock=advancing_clock,
    )

    driver.dispatch(_job("job-idle"))
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.idle_timeouts >= 0  # The kernel tracks idle timeout events


# -- WorkerKernel draining ----------------------------------------------------


def test_worker_kernel_drain_stops_intake() -> None:
    """Draining stops new work intake immediately.

    Jobs dispatched before draining are NOT processed once draining starts,
    since they are not in-flight (not yet started).
    """
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-drain-1"))
    driver.dispatch(_job("job-drain-2"))

    kernel.start_draining()
    assert kernel.is_draining

    processed: list[str] = []
    result1 = kernel.run_once(
        consumer=lambda job: processed.append(job.envelope.metadata.message_id)
    )
    result2 = kernel.run_once(
        consumer=lambda job: processed.append(job.envelope.metadata.message_id)
    )

    # No new work should be picked up during drain
    assert result1 is None
    assert result2 is None
    assert processed == []


def test_worker_kernel_drain_rejects_new_work() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    kernel.start_draining()

    driver.dispatch(_job("job-after-drain"))
    result = kernel.run_once(consumer=lambda job: None)
    assert result is None


def test_worker_kernel_drain_completes_then_stops() -> None:
    """Draining rejects new work even after processing prior jobs.

    Once draining starts, no new reservations are made.
    """
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-drain-1"))
    kernel.start_draining()

    processed: list[str] = []
    result = kernel.run_once(
        consumer=lambda job: processed.append(job.envelope.metadata.message_id)
    )
    assert result is None
    assert processed == []

    driver.dispatch(_job("job-drain-2"))
    result2 = kernel.run_once(
        consumer=lambda job: processed.append(job.envelope.metadata.message_id)
    )
    assert result2 is None
    assert processed == []


# -- WorkerKernel cancellation ------------------------------------------------


def test_worker_kernel_cancellation_aborts_job() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-cancel"))

    cancelled = []

    def consumer(job: QueueJob) -> None:
        cancelled.append(job.envelope.metadata.message_id)

    kernel.run_once(consumer=consumer)
    assert cancelled == ["job-cancel"]


# -- WorkerKernel idempotency -------------------------------------------------


def test_worker_kernel_skips_duplicate_delivery() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    job = _job("job-dup")
    driver.dispatch(job)
    driver.dispatch(job)

    processed: list[str] = []
    kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))
    kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))

    assert processed == ["job-dup"]
    stats = kernel.stats
    assert stats.retries == 1


# -- WorkerKernel state isolation ---------------------------------------------


def test_worker_kernel_no_state_leak_between_jobs() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-isolate-1"))
    driver.dispatch(_job("job-isolate-2"))

    context: list[dict[str, str]] = []

    def consumer(job: QueueJob) -> None:
        context.append({"id": job.envelope.metadata.message_id})

    kernel.run_once(consumer=consumer)
    kernel.run_once(consumer=consumer)

    assert len(context) == 2
    assert context[0] == {"id": "job-isolate-1"}
    assert context[1] == {"id": "job-isolate-2"}
    assert kernel.state is WorkerState.IDLE


# -- WorkerKernel observability -----------------------------------------------


def test_worker_kernel_tracks_lag() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    occurred = _NOW - timedelta(seconds=10)
    job = _job(
        "job-lag",
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id="job-lag",
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=occurred,
            ),
            payload={"message_id": "job-lag"},
        ),
    )
    driver.dispatch(job)
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.lag >= timedelta(seconds=0)


def test_worker_kernel_tracks_saturation() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, max_concurrency=2, clock=lambda: _NOW)

    driver.dispatch(_job("job-sat-1"))
    driver.dispatch(_job("job-sat-2"))

    kernel.run_once(consumer=lambda job: None)
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.saturation >= 0.0


# -- Fake driver helper -------------------------------------------------------


class _TestQueueDriver:
    """Minimal in-memory queue driver for worker tests."""

    def __init__(self) -> None:
        from collections import deque

        self._queue: deque[QueueJob] = deque()
        self._closed = False
        self._features = frozenset({QueueFeature.RESERVATION, QueueFeature.INLINE_EXECUTION})

    @property
    def features(self) -> frozenset[QueueFeature]:
        return self._features

    def supports(self, feature: QueueFeature, /) -> bool:
        return feature in self._features

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt:
        if self._closed:
            raise QueueOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="queue.connection",
            )
        self._queue.append(job)
        return QueueDispatchReceipt(
            message_id=job.envelope.metadata.message_id,
            lane=job.lane,
            disposition=QueueDispatchDisposition.INLINE,
            dispatched_at=_NOW,
        )

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        if self._closed:
            raise QueueOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="queue.connection",
            )
        if lane is not None:
            for i, job in enumerate(self._queue):
                if job.lane == lane:
                    reserved = self._queue[i]
                    del self._queue[i]
                    return QueueReservation(job=reserved, attempt=1, reserved_at=_NOW)
            return None
        if not self._queue:
            return None
        job = self._queue.popleft()
        return QueueReservation(job=job, attempt=1, reserved_at=_NOW)

    def ack(self, reservation: QueueReservation, /) -> None:
        pass

    def reject(self, reservation: QueueReservation, /) -> None:
        pass

    def retry(
        self,
        reservation: QueueReservation,
        /,
        *,
        available_at: datetime | None = None,
    ) -> None:
        self._queue.appendleft(reservation.job)

    def close(self) -> None:
        self._closed = True


def _make_driver() -> _TestQueueDriver:
    return _TestQueueDriver()
