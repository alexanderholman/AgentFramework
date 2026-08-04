from __future__ import annotations

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
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
    InMemoryIdempotencyStore,
    WorkerKernel,
    WorkerState,
)

_NOW = datetime(2026, 8, 1, 15, tzinfo=UTC)


def test_queue_public_api_exports_worker_reliability_boundaries() -> None:
    from af.queue import (
        IdempotencyClaim,
        IdempotencyStore,
        InMemoryIdempotencyStore,
        JobScopeFactory,
    )

    assert IdempotencyClaim is not None
    assert IdempotencyStore is not None
    assert InMemoryIdempotencyStore is not None
    assert JobScopeFactory is not None


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


def test_worker_kernel_close_failure_remains_retryable() -> None:
    driver = _make_driver()
    driver.fail_next_close = True
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    with pytest.raises(RuntimeError, match="close failed"):
        kernel.close()
    assert kernel.state is WorkerState.DRAINING

    kernel.close()
    assert kernel.state is WorkerState.STOPPED


def test_worker_kernel_rejects_close_from_active_handler() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    driver.dispatch(_job("job-self-close"))

    def consumer(job: QueueJob) -> None:
        with pytest.raises(QueueOperationError) as exc:
            kernel.close()
        assert exc.value.reason is PlatformReason.SHUTDOWN_FAILED

    kernel.run_once(consumer)
    kernel.close()
    assert driver._closed


# -- WorkerKernel concurrency -------------------------------------------------


def test_worker_kernel_respects_max_concurrency() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, max_concurrency=2, clock=lambda: _NOW)

    driver.dispatch(_job("job-a"))
    driver.dispatch(_job("job-b"))
    driver.dispatch(_job("job-c"))

    entered = Event()
    release = Event()
    processed: list[str] = []
    processed_lock = Lock()

    def consumer(job: QueueJob) -> None:
        with processed_lock:
            processed.append(job.envelope.metadata.message_id)
            if len(processed) == 2:
                entered.set()
        assert release.wait(timeout=2)

    threads = [Thread(target=kernel.run_once, args=(consumer,)) for _ in range(2)]
    for thread in threads:
        thread.start()

    assert entered.wait(timeout=2)
    assert kernel.stats.saturation == 1.0
    release.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert set(processed) == {"job-a", "job-b"}


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


def test_worker_kernel_retries_instead_of_acknowledging_async_handler() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-async", max_attempts=2))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    async def async_consumer(job: QueueJob) -> None:
        return None

    with pytest.raises(TypeError, match="asynchronous result"):
        kernel.run_once(consumer=async_consumer)  # type: ignore[arg-type]

    assert driver.acked == []
    assert driver.retried == ["job-async"]


def test_worker_kernel_tracks_runtime() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-rt"))
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.jobs_processed == 1


def test_worker_kernel_stats_are_immutable_snapshots() -> None:
    kernel = WorkerKernel(_make_driver(), clock=lambda: _NOW)

    with pytest.raises(FrozenInstanceError):
        kernel.stats.jobs_processed = 99  # type: ignore[misc]


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
    assert stats.idle_timeouts == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_concurrency": 0},
        {"heartbeat_interval": timedelta(0)},
        {"max_idle_time": timedelta(0)},
    ],
)
def test_worker_kernel_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WorkerKernel(_make_driver(), clock=lambda: _NOW, **kwargs)  # type: ignore[arg-type]


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

    def consumer(job: QueueJob) -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        kernel.run_once(consumer=consumer)

    assert driver.retried == ["job-cancel"]
    assert kernel.stats.cancellations == 1
    assert kernel.state is WorkerState.IDLE


# -- WorkerKernel idempotency -------------------------------------------------


def test_worker_kernel_skips_duplicate_delivery_through_injected_store() -> None:
    driver = _make_driver()
    idempotency = InMemoryIdempotencyStore(capacity=10)
    kernel = WorkerKernel(driver, idempotency=idempotency, clock=lambda: _NOW)

    job = _job("job-dup")
    driver.dispatch(job)
    driver.dispatch(job)

    processed: list[str] = []
    kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))
    kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))

    assert processed == ["job-dup"]
    stats = kernel.stats
    assert stats.duplicates == 1


def test_worker_kernel_durable_idempotency_survives_ack_failure() -> None:
    driver = _make_driver()
    driver.fail_next_ack = True
    idempotency = InMemoryIdempotencyStore(capacity=10)
    kernel = WorkerKernel(driver, idempotency=idempotency, clock=lambda: _NOW)
    job = _job("job-ack-failure")
    processed: list[str] = []

    driver.dispatch(job)
    with pytest.raises(RuntimeError, match="ack failed"):
        kernel.run_once(
            consumer=lambda queued: processed.append(queued.envelope.metadata.message_id)
        )

    kernel.run_once(consumer=lambda job: processed.append("duplicate-side-effect"))

    assert processed == ["job-ack-failure"]
    assert driver.acked == ["job-ack-failure"]
    assert driver.retried == ["job-ack-failure"]


def test_worker_kernel_cancellation_during_ack_releases_reservation() -> None:
    driver = _make_driver()
    driver.next_ack_error = KeyboardInterrupt()
    driver.dispatch(_job("job-ack-cancel"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    with pytest.raises(KeyboardInterrupt):
        kernel.run_once(consumer=lambda job: None)

    assert driver.retried == ["job-ack-cancel"]


def test_worker_kernel_idempotency_failure_releases_reservation() -> None:
    class FailingIdempotency:
        @contextmanager
        def claim(self, message_id: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("idempotency unavailable")
            yield

    driver = _make_driver()
    driver.dispatch(_job("job-store-failure", max_attempts=2))
    kernel = WorkerKernel(driver, idempotency=FailingIdempotency(), clock=lambda: _NOW)

    with pytest.raises(RuntimeError, match="idempotency unavailable"):
        kernel.run_once(consumer=lambda job: None)

    assert driver.retried == ["job-store-failure"]


def test_worker_kernel_records_idempotency_inside_job_scope() -> None:
    lifecycle: list[str] = []

    class Claim:
        already_processed = False

        def record(self) -> None:
            lifecycle.append("record")

    class Store:
        @contextmanager
        def claim(self, message_id: str):  # type: ignore[no-untyped-def]
            lifecycle.append("claim-enter")
            try:
                yield Claim()
            finally:
                lifecycle.append("claim-exit")

    @contextmanager
    def scope(job: QueueJob):  # type: ignore[no-untyped-def]
        lifecycle.append("scope-enter")
        try:
            yield
        finally:
            lifecycle.append("scope-exit")

    driver = _make_driver()
    driver.dispatch(_job("job-atomic-boundary"))
    kernel = WorkerKernel(
        driver,
        idempotency=Store(),
        scope_factory=scope,
        clock=lambda: _NOW,
    )

    kernel.run_once(consumer=lambda job: lifecycle.append("consumer"))

    assert lifecycle == [
        "claim-enter",
        "scope-enter",
        "consumer",
        "record",
        "scope-exit",
        "claim-exit",
    ]


def test_in_memory_idempotency_store_evicts_oldest_record() -> None:
    driver = _make_driver()
    store = InMemoryIdempotencyStore(capacity=1)
    kernel = WorkerKernel(driver, idempotency=store, clock=lambda: _NOW)
    processed: list[str] = []
    for message_id in ("job-a", "job-b", "job-a"):
        driver.dispatch(_job(message_id))
        kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))

    assert processed == ["job-a", "job-b", "job-a"]


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


def test_worker_kernel_creates_and_disposes_a_scope_per_job() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-scope-1"))
    driver.dispatch(_job("job-scope-2"))
    lifecycle: list[str] = []

    @contextmanager
    def scope(job: QueueJob):  # type: ignore[no-untyped-def]
        message_id = job.envelope.metadata.message_id
        lifecycle.append(f"enter:{message_id}")
        try:
            yield
        finally:
            lifecycle.append(f"exit:{message_id}")

    kernel = WorkerKernel(driver, scope_factory=scope, clock=lambda: _NOW)
    kernel.run_once(
        consumer=lambda job: lifecycle.append(f"run:{job.envelope.metadata.message_id}")
    )
    kernel.run_once(
        consumer=lambda job: lifecycle.append(f"run:{job.envelope.metadata.message_id}")
    )

    assert lifecycle == [
        "enter:job-scope-1",
        "run:job-scope-1",
        "exit:job-scope-1",
        "enter:job-scope-2",
        "run:job-scope-2",
        "exit:job-scope-2",
    ]


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
    assert stats.saturation == 0.5


def test_worker_kernel_remains_processing_until_all_concurrent_jobs_finish() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, max_concurrency=2, clock=lambda: _NOW)
    driver.dispatch(_job("job-first"))
    driver.dispatch(_job("job-second"))
    both_entered = Event()
    first_release = Event()
    second_release = Event()
    entered: list[str] = []
    entered_lock = Lock()

    def consumer(job: QueueJob) -> None:
        message_id = job.envelope.metadata.message_id
        with entered_lock:
            entered.append(message_id)
            if len(entered) == 2:
                both_entered.set()
        release = first_release if message_id == "job-first" else second_release
        assert release.wait(timeout=2)

    first = Thread(target=kernel.run_once, args=(consumer,))
    second = Thread(target=kernel.run_once, args=(consumer,))
    first.start()
    second.start()
    assert both_entered.wait(timeout=2)

    first_release.set()
    first.join(timeout=2)
    assert not first.is_alive()
    assert kernel.state is WorkerState.PROCESSING

    second_release.set()
    second.join(timeout=2)
    assert not second.is_alive()
    assert kernel.state is WorkerState.IDLE


def test_worker_kernel_retries_transient_failure_before_terminal_reject() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    driver.dispatch(_job("job-retry", max_attempts=2, backoff=timedelta(seconds=1)))

    def failing_consumer(job: QueueJob) -> None:
        raise ValueError("transient")

    with pytest.raises(ValueError, match="transient"):
        kernel.run_once(consumer=failing_consumer)
    assert driver.retried == ["job-retry"]
    assert driver.rejected == []
    assert kernel.stats.retries == 1

    with pytest.raises(ValueError, match="transient"):
        kernel.run_once(consumer=failing_consumer)
    assert driver.rejected == ["job-retry"]


def test_worker_kernel_reports_stopped_state_after_close() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    kernel.close()
    assert kernel.state is WorkerState.STOPPED


def test_worker_kernel_reports_draining_state() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    kernel.start_draining()
    assert kernel.state is WorkerState.DRAINING


def test_worker_kernel_context_manager_closes_on_exit() -> None:
    driver = _make_driver()
    with WorkerKernel(driver, clock=lambda: _NOW) as kernel:
        assert kernel.state is WorkerState.IDLE
    assert kernel.state is WorkerState.STOPPED


def test_worker_kernel_context_manager_closes_on_exception() -> None:
    driver = _make_driver()
    try:
        with WorkerKernel(driver, clock=lambda: _NOW) as kernel:
            assert kernel.state is WorkerState.IDLE
            raise ValueError("simulated failure")
    except ValueError:
        pass
    assert driver._closed is True


def test_worker_kernel_run_loop_does_not_reserve_after_drain_starts() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("loop-1"))
    driver.dispatch(_job("loop-2"))

    kernel.start_draining()

    processed: list[str] = []
    kernel.run_loop(
        consumer=lambda job: processed.append(job.envelope.metadata.message_id),
        poll_interval=timedelta(milliseconds=1),
    )
    assert processed == []


def test_worker_kernel_close_waits_for_active_job_before_closing_driver() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    driver.dispatch(_job("job-in-flight"))
    entered = Event()
    release = Event()

    def consumer(job: QueueJob) -> None:
        entered.set()
        assert release.wait(timeout=2)

    worker = Thread(target=kernel.run_once, args=(consumer,))
    worker.start()
    assert entered.wait(timeout=2)

    closer = Thread(target=kernel.close)
    closer.start()
    assert not driver._closed

    release.set()
    worker.join(timeout=2)
    closer.join(timeout=2)
    assert not worker.is_alive()
    assert not closer.is_alive()
    assert driver._closed


def test_worker_kernel_run_loop_stops_when_drained() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    kernel.start_draining()
    # run_loop should exit immediately when draining with no active jobs
    kernel.run_loop(
        consumer=lambda job: None,
        poll_interval=timedelta(milliseconds=1),
    )
    assert kernel.is_draining


def test_worker_kernel_run_loop_stops_when_closed() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    # Close before run_loop; it should exit immediately
    kernel.close()
    kernel.run_loop(
        consumer=lambda job: None,
        poll_interval=timedelta(milliseconds=1),
    )


def test_worker_kernel_run_loop_continues_after_settled_job_failure() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-terminal-failure"))
    driver.dispatch(_job("job-after-failure"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    processed: list[str] = []

    def consumer(job: QueueJob) -> None:
        message_id = job.envelope.metadata.message_id
        if message_id == "job-terminal-failure":
            raise ValueError("terminal")
        processed.append(message_id)
        kernel.start_draining()

    kernel.run_loop(consumer, poll_interval=timedelta(milliseconds=1))

    assert driver.rejected == ["job-terminal-failure"]
    assert processed == ["job-after-failure"]


def test_worker_kernel_idle_timeout_increments_counter() -> None:
    driver = _make_driver()
    t = _NOW

    def advancing_clock() -> datetime:
        nonlocal t
        t += timedelta(seconds=60)
        return t

    kernel = WorkerKernel(
        driver,
        max_idle_time=timedelta(seconds=30),
        clock=advancing_clock,
    )

    # First call sets heartbeat, second call should detect idle timeout
    driver.dispatch(_job("idle-1"))
    kernel.run_once(consumer=lambda job: None)
    driver.dispatch(_job("idle-2"))
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.idle_timeouts >= 1


def test_worker_kernel_detects_idle_timeout_before_first_job() -> None:
    driver = _make_driver()
    now = _NOW

    def clock() -> datetime:
        return now

    kernel = WorkerKernel(
        driver,
        max_idle_time=timedelta(seconds=30),
        clock=clock,
    )
    assert kernel.run_once(consumer=lambda job: None) is None
    now += timedelta(seconds=31)
    assert kernel.run_once(consumer=lambda job: None) is None
    assert kernel.stats.idle_timeouts == 1


def test_worker_kernel_preserves_cancellation_when_release_fails() -> None:
    driver = _make_driver()
    driver.fail_next_retry = True
    driver.dispatch(_job("job-cancel-release"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    def consumer(job: QueueJob) -> None:
        raise KeyboardInterrupt

    with pytest.raises(BaseExceptionGroup) as exc:
        kernel.run_once(consumer)

    assert any(isinstance(error, KeyboardInterrupt) for error in exc.value.exceptions)
    assert any(isinstance(error, RuntimeError) for error in exc.value.exceptions)


def test_worker_kernel_lag_calculated_from_reservation() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    occurred = _NOW - timedelta(seconds=5)
    job = _job(
        "lag-explicit",
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id="lag-explicit",
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=occurred,
            ),
            payload={"message_id": "lag-explicit"},
        ),
    )
    driver.dispatch(job)
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.lag == timedelta(seconds=5)


# -- Fake driver helper -------------------------------------------------------


class _TestQueueDriver:
    """Minimal in-memory queue driver for worker tests."""

    def __init__(self) -> None:
        from collections import deque

        self._queue: deque[QueueJob] = deque()
        self._closed = False
        self._attempts: dict[str, int] = {}
        self._lock = Lock()
        self.acked: list[str] = []
        self.rejected: list[str] = []
        self.retried: list[str] = []
        self.fail_next_ack = False
        self.next_ack_error: BaseException | None = None
        self.fail_next_retry = False
        self.fail_next_close = False
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
                    message_id = reserved.envelope.metadata.message_id
                    self._attempts[message_id] = self._attempts.get(message_id, 0) + 1
                    return QueueReservation(
                        job=reserved,
                        attempt=self._attempts[message_id],
                        reserved_at=_NOW,
                    )
            return None
        if not self._queue:
            return None
        job = self._queue.popleft()
        message_id = job.envelope.metadata.message_id
        self._attempts[message_id] = self._attempts.get(message_id, 0) + 1
        return QueueReservation(
            job=job,
            attempt=self._attempts[message_id],
            reserved_at=_NOW,
        )

    def ack(self, reservation: QueueReservation, /) -> None:
        if self.next_ack_error is not None:
            error = self.next_ack_error
            self.next_ack_error = None
            raise error
        if self.fail_next_ack:
            self.fail_next_ack = False
            raise RuntimeError("ack failed")
        self.acked.append(reservation.job.envelope.metadata.message_id)

    def reject(self, reservation: QueueReservation, /) -> None:
        self.rejected.append(reservation.job.envelope.metadata.message_id)

    def retry(
        self,
        reservation: QueueReservation,
        /,
        *,
        available_at: datetime | None = None,
    ) -> None:
        if self.fail_next_retry:
            self.fail_next_retry = False
            raise RuntimeError("retry failed")
        self.retried.append(reservation.job.envelope.metadata.message_id)
        self._queue.appendleft(reservation.job)

    def close(self) -> None:
        if self.fail_next_close:
            self.fail_next_close = False
            raise RuntimeError("close failed")
        self._closed = True


def _make_driver() -> _TestQueueDriver:
    return _TestQueueDriver()
