from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import (
    QueueJob,
    QueueReservation,
)
from af.queue.worker_kernel import (
    BatchResult,
    JobTag,
    WorkerKernel,
    WorkerMetrics,
    WorkerState,
)
from af.testing import FakeClock

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
            payload={"message_id": message_id},
        )
    }
    fields.update(overrides)
    return QueueJob(**fields)


class _ReserveDriver:
    """Driver that supports reserve/ack/reject/retry for worker tests."""

    def __init__(
        self,
        jobs: list[QueueJob],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._jobs = list(jobs)
        self._reserved: set[str] = set()
        self._completed: set[str] = set()
        self._failed: set[str] = set()
        self._clock = clock or (lambda: _NOW)
        self._closed = False

    def dispatch(self, job: QueueJob) -> None:
        if self._closed:
            raise RuntimeError("driver closed")
        self._jobs.append(job)

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        if self._closed:
            return None
        for job in self._jobs:
            mid = job.envelope.metadata.message_id
            if mid not in self._reserved and mid not in self._completed and mid not in self._failed:
                self._reserved.add(mid)
                now = self._clock()
                return QueueReservation(
                    job=job,
                    attempt=1,
                    reserved_at=now,
                )
        return None

    def ack(self, reservation: QueueReservation) -> None:
        if self._closed:
            return
        mid = reservation.job.envelope.metadata.message_id
        self._reserved.discard(mid)
        self._completed.add(mid)

    def reject(self, reservation: QueueReservation) -> None:
        if self._closed:
            return
        mid = reservation.job.envelope.metadata.message_id
        self._reserved.discard(mid)
        self._failed.add(mid)

    def retry(self, reservation: QueueReservation, *, available_at: datetime | None = None) -> None:
        if self._closed:
            return
        mid = reservation.job.envelope.metadata.message_id
        self._reserved.discard(mid)
        self._jobs.append(reservation.job)

    def close(self) -> None:
        self._closed = True


# -- State machine tests --


def test_worker_kernel_initial_state() -> None:
    driver = _ReserveDriver([], clock=FakeClock(_NOW))
    kernel = WorkerKernel(driver, handler=lambda j: None, clock=FakeClock(_NOW))

    assert kernel.state is WorkerState.IDLE
    assert kernel.metrics is not None
    assert kernel.metrics.jobs_processed == 0
    assert kernel.metrics.jobs_failed == 0


def test_worker_kernel_boot_starts_processing() -> None:
    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver([_job("job-1"), _job("job-2")], clock=clock)

    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job),
        clock=clock,
    )

    kernel.boot()
    assert kernel.state is WorkerState.RUNNING

    processed.clear()
    kernel.process_one()
    assert len(processed) == 1
    assert processed[0].envelope.metadata.message_id == "job-1"

    kernel.process_one()
    assert len(processed) == 2
    assert processed[1].envelope.metadata.message_id == "job-2"

    kernel.process_one()
    assert len(processed) == 2
    assert kernel.state is WorkerState.IDLE


def test_worker_kernel_shutdown_stops_intake() -> None:
    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver([_job("job-1")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job),
        clock=clock,
    )

    kernel.boot()
    kernel.process_one()
    kernel.shutdown()

    assert kernel.state is WorkerState.STOPPED
    driver.dispatch(_job("job-after-shutdown"))
    kernel.process_one()
    assert len(processed) == 1


def test_worker_kernel_boot_twice_is_noop() -> None:
    clock = FakeClock(_NOW)
    driver = _ReserveDriver([], clock=clock)
    kernel = WorkerKernel(driver, handler=lambda j: None, clock=clock)

    kernel.boot()
    kernel.boot()
    assert kernel.state is WorkerState.RUNNING


def test_worker_kernel_shutdown_twice_is_noop() -> None:
    clock = FakeClock(_NOW)
    driver = _ReserveDriver([], clock=clock)
    kernel = WorkerKernel(driver, handler=lambda j: None, clock=clock)

    kernel.boot()
    kernel.shutdown()
    kernel.shutdown()
    assert kernel.state is WorkerState.STOPPED


def test_worker_kernel_process_before_boot_is_noop() -> None:
    clock = FakeClock(_NOW)
    driver = _ReserveDriver([_job("job-early")], clock=clock)
    kernel = WorkerKernel(driver, handler=lambda j: None, clock=clock)

    kernel.process_one()
    assert kernel.metrics.jobs_processed == 0


def test_worker_kernel_handles_empty_queue() -> None:
    clock = FakeClock(_NOW)
    driver = _ReserveDriver([], clock=clock)
    kernel = WorkerKernel(driver, handler=lambda j: None, clock=clock)
    kernel.boot()

    kernel.process_one()
    assert kernel.metrics.jobs_processed == 0
    assert kernel.state is WorkerState.IDLE


# -- Idempotency and isolation --


def test_worker_kernel_idempotent_duplicate_delivery() -> None:
    clock = FakeClock(_NOW)
    processed: list[str] = []
    driver = _ReserveDriver(
        [_job("job-dup"), _job("job-dup")],
        clock=clock,
    )
    seen: set[str] = set()

    def handler(job: QueueJob) -> None:
        mid = job.envelope.metadata.message_id
        if mid not in seen:
            seen.add(mid)
            processed.append(mid)

    kernel = WorkerKernel(driver, handler=handler, clock=clock)
    kernel.boot()

    kernel.process_one()
    kernel.process_one()

    assert processed == ["job-dup"]


def test_worker_kernel_no_state_leak_between_jobs() -> None:
    clock = FakeClock(_NOW)
    handler_state: list[dict[str, Any]] = []

    def stateful_handler(job: QueueJob) -> None:
        ctx: dict[str, Any] = {}
        ctx["job_id"] = job.envelope.metadata.message_id
        handler_state.append(ctx)

    driver = _ReserveDriver(
        [_job("job-alpha"), _job("job-beta")],
        clock=clock,
    )
    kernel = WorkerKernel(driver, handler=stateful_handler, clock=clock)
    kernel.boot()

    kernel.process_one()
    kernel.process_one()

    assert handler_state[0] == {"job_id": "job-alpha"}
    assert handler_state[1] == {"job_id": "job-beta"}


# -- Failure handling --


def test_worker_kernel_handles_handler_failure() -> None:
    clock = FakeClock(_NOW)
    call_count = 0

    def flaky_handler(job: QueueJob) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("transient failure")

    driver = _ReserveDriver([_job("job-fail"), _job("job-ok")], clock=clock)
    kernel = WorkerKernel(driver, handler=flaky_handler, clock=clock)
    kernel.boot()

    kernel.process_one()
    assert kernel.metrics.jobs_failed == 1
    assert kernel.metrics.jobs_processed == 0

    kernel.process_one()
    assert kernel.metrics.jobs_processed == 1
    assert kernel.metrics.jobs_failed == 1


def test_worker_kernel_failed_job_replay() -> None:
    clock = FakeClock(_NOW)
    call_count = 0

    def flaky(job: QueueJob) -> None:
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            raise RuntimeError("fail first time")

    driver = _ReserveDriver([_job("job-replay")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=flaky,
        max_retries=2,
        clock=clock,
    )
    kernel.boot()

    kernel.process_one()
    assert kernel.metrics.jobs_failed == 1
    assert kernel.metrics.retries == 1

    kernel.process_one()
    assert kernel.metrics.jobs_processed == 1
    assert kernel.metrics.retries == 1


# -- Batch processing --


def test_worker_kernel_process_batch() -> None:
    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver(
        [_job("job-1"), _job("job-2"), _job("job-3")],
        clock=clock,
    )
    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job),
        max_concurrency=3,
        clock=clock,
    )
    kernel.boot()

    result = kernel.process_batch(max_jobs=2)
    assert result.jobs_processed == 2
    assert result.jobs_failed == 0
    assert len(processed) == 2

    result2 = kernel.process_batch(max_jobs=10)
    assert result2.jobs_processed == 1
    assert len(processed) == 3


# -- Concurrency and backpressure --


def test_worker_kernel_concurrency_limit() -> None:
    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver(
        [_job("job-1"), _job("job-2"), _job("job-3"), _job("job-4")],
        clock=clock,
    )
    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job),
        max_concurrency=2,
        clock=clock,
    )
    kernel.boot()

    result = kernel.process_batch(max_jobs=10)
    assert result.jobs_processed == 2
    assert kernel.metrics.active_workers == 0


def test_worker_kernel_backpressure_when_concurrency_reached() -> None:
    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver(
        [_job("job-bp1"), _job("job-bp2"), _job("job-bp3")],
        clock=clock,
    )
    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job),
        max_concurrency=2,
        clock=clock,
    )
    kernel.boot()

    result = kernel.process_batch(max_jobs=10)
    assert result.jobs_processed == 2
    assert len(processed) == 2


# -- Metrics --


def test_worker_kernel_metrics_track_throughput() -> None:
    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver([_job("job-m1"), _job("job-m2")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job),
        clock=clock,
    )
    kernel.boot()
    kernel.process_one()
    kernel.process_one()

    m = kernel.metrics
    assert m.jobs_processed == 2
    assert m.jobs_failed == 0
    assert m.retries == 0
    assert m.runtime.total_seconds() >= 0


def test_worker_kernel_metrics_track_saturation() -> None:
    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver(
        [_job("job-s1"), _job("job-s2"), _job("job-s3")],
        clock=clock,
    )
    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job),
        max_concurrency=1,
        clock=clock,
    )
    kernel.boot()
    kernel.process_one()

    assert kernel.metrics.active_workers == 0
    assert kernel.metrics.max_concurrency == 1


# -- Job tagging --


def test_worker_kernel_job_tagging() -> None:
    clock = FakeClock(_NOW)
    processed: list[JobTag] = []
    driver = _ReserveDriver([_job("job-tagged")], clock=clock)

    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(kernel.build_tag(job)),
        clock=clock,
    )
    kernel.boot()
    kernel.process_one()

    assert len(processed) == 1
    tag = processed[0]
    assert tag.correlation_id == "job-tagged"


# -- Drain on shutdown --


def test_worker_kernel_shutdown_drains_in_flight() -> None:
    clock = FakeClock(_NOW)
    completed: list[QueueJob] = []

    def slow_handler(job: QueueJob) -> None:
        completed.append(job)

    driver = _ReserveDriver([_job("job-drain")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=slow_handler,
        clock=clock,
    )
    kernel.boot()
    kernel.process_one()

    assert len(completed) == 1
    kernel.shutdown()
    assert kernel.state is WorkerState.STOPPED


# -- WorkerSupervisor tests --


def test_worker_supervisor_creates_kernels() -> None:
    from af.queue.worker_kernel import WorkerSupervisor

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([], clock=clock)
    supervisor = WorkerSupervisor(
        driver,
        handler=lambda j: None,
        num_workers=2,
        clock=clock,
    )

    assert len(supervisor.kernels) == 2
    for k in supervisor.kernels:
        assert k.state is WorkerState.IDLE


def test_worker_supervisor_boot_starts_all() -> None:
    from af.queue.worker_kernel import WorkerSupervisor

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([], clock=clock)
    supervisor = WorkerSupervisor(
        driver,
        handler=lambda j: None,
        num_workers=3,
        clock=clock,
    )

    supervisor.boot()
    for k in supervisor.kernels:
        assert k.state is WorkerState.RUNNING


def test_worker_supervisor_shutdown_stops_all() -> None:
    from af.queue.worker_kernel import WorkerSupervisor

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([], clock=clock)
    supervisor = WorkerSupervisor(
        driver,
        handler=lambda j: None,
        num_workers=2,
        clock=clock,
    )

    supervisor.boot()
    supervisor.shutdown()
    for k in supervisor.kernels:
        assert k.state is WorkerState.STOPPED


def test_worker_supervisor_process_distributes_jobs() -> None:
    from af.queue.worker_kernel import WorkerSupervisor

    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver(
        [_job("job-dist-1"), _job("job-dist-2"), _job("job-dist-3")],
        clock=clock,
    )
    supervisor = WorkerSupervisor(
        driver,
        handler=lambda job: processed.append(job),
        num_workers=2,
        max_concurrency=2,
        clock=clock,
    )

    supervisor.boot()
    supervisor.process()
    assert len(processed) == 3


def test_worker_supervisor_aggregated_metrics() -> None:
    from af.queue.worker_kernel import WorkerSupervisor

    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver(
        [_job("job-m1"), _job("job-m2")],
        clock=clock,
    )
    supervisor = WorkerSupervisor(
        driver,
        handler=lambda job: processed.append(job),
        num_workers=2,
        clock=clock,
    )

    supervisor.boot()
    supervisor.process()

    agg = supervisor.metrics
    assert agg.jobs_processed == 2
    assert agg.jobs_failed == 0


def test_worker_supervisor_restarts_failed_kernel() -> None:
    from af.queue.worker_kernel import WorkerSupervisor

    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []

    def failing_once(job: QueueJob) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("crash")
        processed.append(job)

    call_count = 0
    driver = _ReserveDriver([_job("job-crash"), _job("job-ok")], clock=clock)
    supervisor = WorkerSupervisor(
        driver,
        handler=failing_once,
        num_workers=1,
        max_retries=0,
        clock=clock,
    )

    supervisor.boot()
    supervisor.process()
    # After restart, the kernel should still be running
    assert supervisor.kernels[0].state is WorkerState.RUNNING


# -- Cancellation tests --


def test_worker_kernel_cancel_pending_jobs() -> None:
    from af.queue.worker_kernel import CancellationPolicy

    clock = FakeClock(_NOW)
    processed: list[QueueJob] = []
    driver = _ReserveDriver(
        [_job("job-cancel-1"), _job("job-cancel-2")],
        clock=clock,
    )
    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job),
        clock=clock,
        cancellation=CancellationPolicy.immediate,
    )

    kernel.boot()
    kernel.process_one()
    kernel.cancel()

    assert len(processed) == 1
    kernel.process_one()
    assert len(processed) == 1


def test_worker_kernel_cancel_clears_seen_set() -> None:
    from af.queue.worker_kernel import CancellationPolicy

    clock = FakeClock(_NOW)
    processed: list[str] = []
    driver = _ReserveDriver([_job("job-cancel-clear")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=lambda job: processed.append(job.envelope.metadata.message_id),
        clock=clock,
        cancellation=CancellationPolicy.immediate,
    )

    kernel.boot()
    kernel.process_one()
    assert len(processed) == 1

    kernel.cancel()
    # After cancel, the job should be reprocessable
    driver.dispatch(_job("job-cancel-clear"))
    kernel.process_one()
    assert len(processed) == 2


# -- Heartbeat tests --


def test_worker_kernel_heartbeat_on_process() -> None:
    from af.queue.worker_kernel import HeartbeatConfig

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([_job("job-hb")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=lambda j: None,
        clock=clock,
        heartbeat=HeartbeatConfig(interval=timedelta(seconds=10)),
    )

    kernel.boot()
    kernel.process_one()

    assert kernel.last_heartbeat is not None
    assert kernel.last_heartbeat == _NOW


def test_worker_kernel_stale_detection() -> None:
    from af.queue.worker_kernel import HeartbeatConfig

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([_job("job-stale")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=lambda j: None,
        clock=clock,
        heartbeat=HeartbeatConfig(interval=timedelta(seconds=5)),
    )

    kernel.boot()
    kernel.process_one()

    # Advance time past the heartbeat interval
    clock.advance(timedelta(seconds=6))
    assert kernel.is_stale is True


def test_worker_kernel_not_stale_within_interval() -> None:
    from af.queue.worker_kernel import HeartbeatConfig

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([_job("job-fresh")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=lambda j: None,
        clock=clock,
        heartbeat=HeartbeatConfig(interval=timedelta(seconds=10)),
    )

    kernel.boot()
    kernel.process_one()

    clock.advance(timedelta(seconds=5))
    assert kernel.is_stale is False


# -- Queue wait alert tests --


def test_worker_kernel_queue_wait_alert() -> None:
    from af.queue.worker_kernel import WaitAlertConfig, WaitAlertLevel

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([_job("job-wait")], clock=clock)
    alert_fired: list[str] = []

    def alert_fn(level: WaitAlertLevel) -> None:
        alert_fired.append(level.value)

    kernel = WorkerKernel(
        driver,
        handler=lambda j: None,
        clock=clock,
        wait_alert=WaitAlertConfig(
            warning_threshold=timedelta(seconds=30),
            critical_threshold=timedelta(seconds=60),
            on_alert=alert_fn,
        ),
    )

    kernel.boot()
    # Dispatch a job with an old occurred_at to trigger warning alert
    old_time = _NOW - timedelta(seconds=45)
    old_job = QueueJob(
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id="job-old",
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=old_time,
            ),
            payload={"message_id": "job-old"},
        ),
    )
    driver.dispatch(old_job)
    kernel.process_one()

    assert "warning" in alert_fired


def test_worker_kernel_no_alert_within_threshold() -> None:
    from af.queue.worker_kernel import WaitAlertConfig, WaitAlertLevel

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([_job("job-fresh-wait")], clock=clock)
    alert_fired: list[str] = []

    kernel = WorkerKernel(
        driver,
        handler=lambda j: None,
        clock=clock,
        wait_alert=WaitAlertConfig(
            warning_threshold=timedelta(seconds=60),
            critical_threshold=timedelta(seconds=120),
            on_alert=lambda level: alert_fired.append(level.value),
        ),
    )

    kernel.boot()
    kernel.process_one()

    assert len(alert_fired) == 0


# -- Failed job inspection tests --


def test_worker_kernel_failed_job_inspection() -> None:
    clock = FakeClock(_NOW)

    def failing(job: QueueJob) -> None:
        raise RuntimeError("always fails")

    driver = _ReserveDriver([_job("job-inspect-1"), _job("job-inspect-2")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=failing,
        max_retries=0,
        clock=clock,
    )

    kernel.boot()
    kernel.process_one()
    kernel.process_one()

    assert len(kernel.failed_jobs) == 2
    ids = {fj.message_id for fj in kernel.failed_jobs}
    assert ids == {"job-inspect-1", "job-inspect-2"}


def test_worker_kernel_failed_job_has_error_info() -> None:
    clock = FakeClock(_NOW)

    def failing(job: QueueJob) -> None:
        raise ValueError("test error")

    driver = _ReserveDriver([_job("job-error-info")], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=failing,
        max_retries=0,
        clock=clock,
    )

    kernel.boot()
    kernel.process_one()

    assert len(kernel.failed_jobs) == 1
    fj = kernel.failed_jobs[0]
    assert fj.message_id == "job-error-info"
    assert "test error" in fj.error_message


# -- Optimization provider tests --


def test_worker_kernel_optimization_provider() -> None:
    from af.queue.worker_kernel import OptimizationProvider

    clock = FakeClock(_NOW)

    class FastProvider(OptimizationProvider):
        @property
        def recommended_concurrency(self) -> int:
            return 5

    provider = FastProvider()
    driver = _ReserveDriver([], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=lambda j: None,
        clock=clock,
        provider=provider,
    )

    assert kernel.provider is provider
    assert kernel.provider.recommended_concurrency == 5


# -- Connection recycling tests --


def test_worker_kernel_recycles_connection_on_close() -> None:
    clock = FakeClock(_NOW)
    driver = _ReserveDriver([], clock=clock)
    kernel = WorkerKernel(
        driver,
        handler=lambda j: None,
        clock=clock,
    )

    kernel.boot()
    kernel.shutdown()
    kernel.recycle()

    assert driver._closed is True


def test_worker_supervisor_recycle_all_connections() -> None:
    from af.queue.worker_kernel import WorkerSupervisor

    clock = FakeClock(_NOW)
    driver = _ReserveDriver([], clock=clock)
    supervisor = WorkerSupervisor(
        driver,
        handler=lambda j: None,
        num_workers=2,
        clock=clock,
    )

    supervisor.boot()
    supervisor.shutdown()
    supervisor.recycle()

    assert driver._closed is True
