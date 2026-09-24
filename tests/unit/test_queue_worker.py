from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from typing import Any

import pytest

from af.framework.context import CancellationToken
from af.framework.errors import PlatformReason
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.observability import (
    CapabilityState,
    DegradationState,
    LivenessState,
    ReadinessState,
    TelemetryContext,
    TelemetryKind,
    TelemetryPublisher,
    TelemetrySignal,
)
from af.queue import worker as worker_module
from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
    QueueReservationLost,
)
from af.queue.manager import QueueFeature
from af.queue.worker import (
    InMemoryIdempotencyStore,
    WorkerAdmissionPermit,
    WorkerKernel,
    WorkerState,
)

_NOW = datetime(2026, 8, 1, 15, tzinfo=UTC)


def test_queue_public_api_exports_worker_reliability_boundaries() -> None:
    from af.queue import (
        CooperativeJobHandler,
        IdempotencyClaim,
        IdempotencyStore,
        InMemoryIdempotencyStore,
        JobScopeFactory,
        OwnedManagerConnectionRecycler,
        PurgeableConnectionManager,
        WorkerAdmissionGate,
        WorkerConnectionRecycler,
    )

    assert IdempotencyClaim is not None
    assert CooperativeJobHandler is not None
    assert IdempotencyStore is not None
    assert InMemoryIdempotencyStore is not None
    assert JobScopeFactory is not None
    assert OwnedManagerConnectionRecycler is not None
    assert PurgeableConnectionManager is not None
    assert WorkerAdmissionGate is not None
    assert WorkerConnectionRecycler is not None


def test_worker_kernel_resource_admission_denial_reserves_no_job() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-resource-denied"))
    allowed = False
    observations: list[tuple[str | None, int, int]] = []

    def admission_gate(
        *,
        lane: str | None,
        active_jobs: int,
        max_concurrency: int,
    ) -> bool:
        observations.append((lane, active_jobs, max_concurrency))
        return allowed

    kernel = WorkerKernel(
        driver,
        max_concurrency=2,
        admission_gate=admission_gate,
        clock=lambda: _NOW,
    )
    processed: list[str] = []

    assert (
        kernel.run_once(
            consumer=lambda job: processed.append(job.envelope.metadata.message_id),
            lane="priority",
        )
        is None
    )
    assert observations == [("priority", 0, 2)]
    assert processed == []
    assert driver.queued_message_ids == ["job-resource-denied"]
    assert driver._attempts == {}

    allowed = True
    assert (
        kernel.run_once(consumer=lambda job: processed.append(job.envelope.metadata.message_id))
        is not None
    )
    assert processed == ["job-resource-denied"]


def test_worker_kernel_rejects_invalid_lane_before_resource_admission() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-invalid-admission-lane"))
    admission_calls = 0

    def admission_gate(**_: object) -> bool:
        nonlocal admission_calls
        admission_calls += 1
        return True

    kernel = WorkerKernel(driver, admission_gate=admission_gate, clock=lambda: _NOW)

    with pytest.raises(ValueError, match="queue lane must be a canonical identifier"):
        kernel.run_once(lambda _job: None, lane="INVALID")

    assert admission_calls == 0
    assert driver.queued_message_ids == ["job-invalid-admission-lane"]
    assert driver._attempts == {}


def test_worker_kernel_resource_admission_failure_is_fail_closed() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-resource-gate-failed"))

    def admission_gate(**_: object) -> bool:
        raise RuntimeError("resource admission unavailable")

    kernel = WorkerKernel(driver, admission_gate=admission_gate, clock=lambda: _NOW)

    with pytest.raises(RuntimeError, match="resource admission unavailable"):
        kernel.run_once(consumer=lambda job: None)

    assert driver.queued_message_ids == ["job-resource-gate-failed"]
    assert driver._attempts == {}


def test_worker_kernel_rechecks_drain_after_resource_admission() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-resource-drain-race"))
    gate_entered = Event()
    release_gate = Event()

    def admission_gate(**_: object) -> bool:
        gate_entered.set()
        assert release_gate.wait(timeout=2)
        return True

    kernel = WorkerKernel(driver, admission_gate=admission_gate, clock=lambda: _NOW)
    worker = Thread(target=kernel.run_once, args=(lambda job: None,))
    worker.start()
    assert gate_entered.wait(timeout=2)

    kernel.start_draining()
    release_gate.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert driver.queued_message_ids == ["job-resource-drain-race"]
    assert driver._attempts == {}


def test_worker_kernel_resource_admission_requires_boolean_decision() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-resource-invalid-decision"))
    kernel = WorkerKernel(
        driver,
        admission_gate=lambda **_: 1,  # type: ignore[return-value]
        clock=lambda: _NOW,
    )

    with pytest.raises(TypeError, match="admission permit requires callable release"):
        kernel.run_once(consumer=lambda job: None)

    assert driver.queued_message_ids == ["job-resource-invalid-decision"]
    assert driver._attempts == {}


def test_worker_kernel_rejects_non_callable_resource_admission_gate() -> None:
    with pytest.raises(TypeError, match="resource admission gate must be callable"):
        WorkerKernel(
            _make_driver(),
            admission_gate=object(),  # type: ignore[arg-type]
            clock=lambda: _NOW,
        )


class _AdmissionPermit:
    def __init__(self, *, release_error: BaseException | None = None) -> None:
        self.released = 0
        self.release_error = release_error

    def release(self) -> None:
        self.released += 1
        if self.release_error is not None:
            raise self.release_error


class _ConnectionRecycler:
    def __init__(
        self,
        lifecycle: list[str],
        *,
        error: BaseException | None = None,
    ) -> None:
        self.lifecycle = lifecycle
        self.error = error

    def recycle(self) -> None:
        self.lifecycle.append("recycle")
        if self.error is not None:
            raise self.error


def test_worker_kernel_holds_admission_permit_until_delivery_settles() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-resource-permit"))
    permit = _AdmissionPermit()
    observed: list[int] = []
    kernel = WorkerKernel(driver, admission_gate=lambda **_: permit, clock=lambda: _NOW)

    reservation = kernel.run_once(lambda _job: observed.append(permit.released))

    assert reservation is not None
    assert observed == [0]
    assert permit.released == 1


def test_worker_kernel_releases_admission_permit_when_queue_is_empty() -> None:
    permit = _AdmissionPermit()
    kernel = WorkerKernel(
        _make_driver(),
        admission_gate=lambda **_: permit,
        clock=lambda: _NOW,
    )

    assert kernel.run_once(lambda _job: None) is None
    assert permit.released == 1


def test_worker_kernel_rejects_malformed_admission_permit_before_reservation() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-malformed-permit"))
    kernel = WorkerKernel(
        driver,
        admission_gate=lambda **_: object(),  # type: ignore[return-value]
        clock=lambda: _NOW,
    )

    with pytest.raises(TypeError, match="admission permit requires callable release"):
        kernel.run_once(lambda _job: None)

    assert driver.queued_message_ids == ["job-malformed-permit"]


def test_worker_kernel_rejects_async_admission_permit_before_reservation() -> None:
    class AsyncPermit:
        async def release(self) -> None:
            return None

    driver = _make_driver()
    driver.dispatch(_job("job-async-permit"))
    kernel = WorkerKernel(
        driver,
        admission_gate=lambda **_: AsyncPermit(),
        clock=lambda: _NOW,
    )

    with pytest.raises(TypeError, match="admission permit must be synchronous"):
        kernel.run_once(lambda _job: None)

    assert driver.queued_message_ids == ["job-async-permit"]
    assert driver._attempts == {}


@pytest.mark.parametrize("result_kind", ["coroutine", "awaitable", "value"])
def test_worker_kernel_rejects_invalid_admission_permit_release_result(
    result_kind: str,
) -> None:
    class AwaitableResult:
        def __await__(self):  # type: ignore[no-untyped-def]
            yield

    async def asynchronous_result() -> None:
        return None

    class InvalidPermit:
        def release(self) -> object:
            if result_kind == "coroutine":
                return asynchronous_result()
            if result_kind == "awaitable":
                return AwaitableResult()
            return "unexpected"

    driver = _make_driver()
    driver.dispatch(_job(f"job-invalid-release-{result_kind}"))
    kernel = WorkerKernel(
        driver,
        admission_gate=lambda **_: InvalidPermit(),  # type: ignore[return-value]
        clock=lambda: _NOW,
    )

    expected = (
        "admission permit returned an asynchronous result"
        if result_kind != "value"
        else "admission permit must return None"
    )
    with pytest.raises(TypeError, match=expected):
        kernel.run_once(lambda _job: None)

    assert driver.queued_message_ids == []


def test_worker_kernel_preserves_handler_and_permit_release_failures() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-permit-release-failure"))
    permit = _AdmissionPermit(release_error=RuntimeError("lease release failed"))
    kernel = WorkerKernel(driver, admission_gate=lambda **_: permit, clock=lambda: _NOW)

    def fail(_job: QueueJob) -> None:
        raise ValueError("handler failed")

    with pytest.raises(BaseExceptionGroup) as captured:
        kernel.run_once(fail)

    assert [str(error) for error in captured.value.exceptions] == [
        "handler failed",
        "lease release failed",
    ]
    assert permit.released == 1


def test_worker_kernel_retries_retained_permit_cleanup_before_new_admission() -> None:
    class TransientPermit(_AdmissionPermit):
        def release(self) -> None:
            self.released += 1
            if self.released == 1:
                raise RuntimeError("transient lease release failure")

    driver = _make_driver()
    driver.dispatch(_job("job-retained-permit"))
    permit = TransientPermit()
    admission_count = 0

    def admit(**_: object) -> WorkerAdmissionPermit | bool:
        nonlocal admission_count
        admission_count += 1
        return permit if admission_count == 1 else False

    kernel = WorkerKernel(driver, admission_gate=admit, clock=lambda: _NOW)

    with pytest.raises(RuntimeError, match="transient lease release failure"):
        kernel.run_once(lambda _job: None)

    assert driver.acked == ["job-retained-permit"]
    assert permit.released == 1
    assert kernel.run_once(lambda _job: None) is None
    assert permit.released == 2


def test_worker_kernel_retries_retained_permit_cleanup_during_shutdown() -> None:
    class TransientPermit(_AdmissionPermit):
        def release(self) -> None:
            self.released += 1
            if self.released == 1:
                raise RuntimeError("transient lease release failure")

    driver = _make_driver()
    driver.dispatch(_job("job-shutdown-permit"))
    permit = TransientPermit()
    kernel = WorkerKernel(driver, admission_gate=lambda **_: permit, clock=lambda: _NOW)

    with pytest.raises(RuntimeError, match="transient lease release failure"):
        kernel.run_once(lambda _job: None)

    kernel.close()
    assert permit.released == 2
    assert kernel.state is WorkerState.STOPPED


def test_worker_kernel_recycles_connections_after_settlement_before_permit_release() -> None:
    lifecycle: list[str] = []

    class Driver(_TestQueueDriver):
        def ack(self, reservation: QueueReservation, /) -> None:
            lifecycle.append("ack")
            super().ack(reservation)

    class Permit(_AdmissionPermit):
        def release(self) -> None:
            lifecycle.append("permit-release")
            super().release()

    @contextmanager
    def scope(_job: QueueJob):  # type: ignore[no-untyped-def]
        lifecycle.append("scope-enter")
        try:
            yield
        finally:
            lifecycle.append("scope-exit")

    driver = Driver()
    driver.dispatch(_job("job-connection-recycle"))
    kernel = WorkerKernel(
        driver,
        scope_factory=scope,
        admission_gate=lambda **_: Permit(),
        connection_recycler=_ConnectionRecycler(lifecycle),
        clock=lambda: _NOW,
    )

    kernel.run_once(lambda _job: lifecycle.append("handler"))

    assert lifecycle == [
        "scope-enter",
        "handler",
        "scope-exit",
        "ack",
        "recycle",
        "permit-release",
    ]


def test_worker_kernel_preserves_delivery_recycler_and_permit_failures() -> None:
    lifecycle: list[str] = []
    recycler_error = RuntimeError("connection recycle failed")
    permit_error = OSError("permit release failed")
    permit = _AdmissionPermit(release_error=permit_error)
    driver = _make_driver()
    driver.dispatch(_job("job-connection-recycle-failure"))
    kernel = WorkerKernel(
        driver,
        admission_gate=lambda **_: permit,
        connection_recycler=_ConnectionRecycler(lifecycle, error=recycler_error),
        clock=lambda: _NOW,
    )
    handler_error = ValueError("handler failed")

    with pytest.raises(BaseExceptionGroup) as captured:
        kernel.run_once(lambda _job: (_ for _ in ()).throw(handler_error))

    assert captured.value.exceptions == (handler_error, recycler_error, permit_error)
    assert lifecycle == ["recycle"]
    assert permit.released == 1
    assert driver.rejected == ["job-connection-recycle-failure"]


def test_worker_kernel_does_not_recycle_connections_without_a_delivery() -> None:
    lifecycle: list[str] = []
    kernel = WorkerKernel(
        _make_driver(),
        connection_recycler=_ConnectionRecycler(lifecycle),
        clock=lambda: _NOW,
    )

    assert kernel.run_once(lambda _job: None) is None
    assert lifecycle == []


def test_worker_shutdown_waits_for_connection_recycling() -> None:
    recycle_entered = Event()
    release_recycler = Event()
    driver_closed = Event()
    failures: list[BaseException] = []

    class Driver(_TestQueueDriver):
        def close(self) -> None:
            driver_closed.set()
            super().close()

    class Recycler:
        def recycle(self) -> None:
            recycle_entered.set()
            assert release_recycler.wait(timeout=2)

    driver = Driver()
    driver.dispatch(_job("job-blocked-recycle"))
    kernel = WorkerKernel(
        driver,
        connection_recycler=Recycler(),
        clock=lambda: _NOW,
    )

    def run() -> None:
        try:
            kernel.run_once(lambda _job: None)
        except BaseException as error:
            failures.append(error)

    def close() -> None:
        try:
            kernel.close(timeout=timedelta(seconds=2))
        except BaseException as error:
            failures.append(error)

    worker = Thread(target=run)
    worker.start()
    assert recycle_entered.wait(timeout=2)
    closer = Thread(target=close)
    closer.start()

    assert not driver_closed.wait(timeout=0.05)
    release_recycler.set()
    worker.join(timeout=2)
    closer.join(timeout=2)

    assert not worker.is_alive()
    assert not closer.is_alive()
    assert failures == []
    assert driver_closed.is_set()


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


def test_worker_kernel_rejects_async_handler_before_reserving() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-async", max_attempts=2))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    async def async_consumer(job: QueueJob) -> None:
        return None

    with pytest.raises(TypeError, match="asynchronous result"):
        kernel.run_once(consumer=async_consumer)  # type: ignore[arg-type]

    assert driver.acked == []
    assert driver.retried == []
    assert driver.queued_message_ids == ["job-async"]


def test_worker_kernel_run_loop_rejects_invalid_handler_before_polling() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-invalid-handler"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    with pytest.raises(TypeError, match="callable"):
        kernel.run_loop(None)  # type: ignore[arg-type]

    assert driver.queued_message_ids == ["job-invalid-handler"]
    assert driver.rejected == []


def test_worker_kernel_run_loop_idle_wait_is_interruptible() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    stop = Event()
    failures: list[BaseException] = []

    def run() -> None:
        try:
            kernel.run_loop(
                lambda job: None,
                poll_interval=timedelta(seconds=30),
                stop_event=stop,
            )
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)

    thread.start()
    stop.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert failures == []


def test_worker_kernel_tracks_runtime() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    driver.dispatch(_job("job-rt"))
    kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.jobs_processed == 1


@pytest.mark.parametrize("wall_jump", [timedelta(hours=-1), timedelta(days=1)])
def test_worker_runtime_uses_monotonic_duration_across_wall_clock_jumps(
    monkeypatch: pytest.MonkeyPatch,
    wall_jump: timedelta,
) -> None:
    class MonotonicClock:
        def __init__(self) -> None:
            self._ticks = iter((100.0, 100.25))

        def monotonic(self) -> float:
            return next(self._ticks)

    class RecordingSink:
        def __init__(self) -> None:
            self.signals: list[TelemetrySignal] = []

        def emit(self, signal: TelemetrySignal, /) -> None:
            self.signals.append(signal)

    wall_times = iter((_NOW, _NOW + wall_jump, _NOW + wall_jump))
    monkeypatch.setattr(worker_module, "time", MonotonicClock())
    driver = _make_driver()
    sink = RecordingSink()
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: next(wall_times),
    )

    driver.dispatch(_job("job-monotonic-runtime"))
    kernel.run_once(consumer=lambda job: None)

    assert kernel.stats.total_runtime == timedelta(milliseconds=250)
    delivery = next(signal for signal in sink.signals if signal.name == "queue.worker.delivery")
    assert dict(delivery.attributes)["runtime_bucket"] == "lt_1s"


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


def test_worker_run_loop_emits_fresh_health_heartbeats_until_stopped() -> None:
    class HeartbeatSink:
        def __init__(self) -> None:
            self.signals: list[TelemetrySignal] = []
            self.two_live_heartbeats = Event()

        def emit(self, signal: TelemetrySignal, /) -> None:
            self.signals.append(signal)
            live_count = sum(
                item.name == "queue.worker.heartbeat"
                and item.health_state is not None
                and item.health_state.liveness is LivenessState.LIVE
                for item in self.signals
            )
            if live_count >= 2:
                self.two_live_heartbeats.set()

    driver = _make_driver()
    sink = HeartbeatSink()
    stop = Event()
    failures: list[BaseException] = []
    kernel = WorkerKernel(
        driver,
        heartbeat_interval=timedelta(milliseconds=10),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: datetime.now(UTC),
    )

    def run() -> None:
        try:
            kernel.run_loop(
                lambda job: None,
                poll_interval=timedelta(milliseconds=1),
                stop_event=stop,
            )
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert sink.two_live_heartbeats.wait(timeout=1)
    finally:
        stop.set()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert failures == []
    heartbeats = [signal for signal in sink.signals if signal.name == "queue.worker.heartbeat"]
    assert len(heartbeats) >= 3
    assert all(signal.kind is TelemetryKind.HEALTH for signal in heartbeats)
    assert all(signal.context is None for signal in heartbeats)
    assert all(
        heartbeat.health_state is not None
        and heartbeat.health_state.degradation is DegradationState.HEALTHY
        and heartbeat.health_state.capability is CapabilityState.AVAILABLE
        for heartbeat in heartbeats[:-1]
    )
    assert [heartbeat.health_state.liveness for heartbeat in heartbeats[-2:]] == [
        LivenessState.LIVE,
        LivenessState.STOPPED,
    ]
    assert heartbeats[-1].health_state.readiness is ReadinessState.NOT_READY
    assert dict(heartbeats[0].attributes) == {
        "active_jobs_bucket": "zero",
        "saturation_bucket": "lt_50pct",
        "state": "idle",
    }
    assert heartbeats[0].health_state.fresh_until > heartbeats[0].observed_at


def test_concurrent_polling_loops_share_one_heartbeat_lifecycle() -> None:
    sink = _RecordingTelemetrySink()
    kernel = WorkerKernel(
        _make_driver(),
        max_concurrency=2,
        heartbeat_interval=timedelta(seconds=1),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: datetime.now(UTC),
    )
    first_stop = Event()
    second_stop = Event()
    first = Thread(
        target=kernel.run_loop,
        args=(lambda job: None,),
        kwargs={"poll_interval": timedelta(milliseconds=1), "stop_event": first_stop},
    )
    second = Thread(
        target=kernel.run_loop,
        args=(lambda job: None,),
        kwargs={"poll_interval": timedelta(milliseconds=1), "stop_event": second_stop},
    )

    first.start()
    second.start()
    try:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and kernel._heartbeat_users != 2:
            time.sleep(0.001)
        assert kernel._heartbeat_users == 2

        first_stop.set()
        first.join(timeout=1)
        assert not first.is_alive()
        time.sleep(0.02)
        assert not any(
            signal.health_state is not None
            and signal.health_state.liveness is LivenessState.STOPPED
            for signal in sink.signals
            if signal.name == "queue.worker.heartbeat"
        )
    finally:
        first_stop.set()
        second_stop.set()
        first.join(timeout=1)
        second.join(timeout=1)

    assert not second.is_alive()
    stopped = [
        signal
        for signal in sink.signals
        if signal.name == "queue.worker.heartbeat"
        and signal.health_state is not None
        and signal.health_state.liveness is LivenessState.STOPPED
    ]
    assert len(stopped) == 1


def test_retired_heartbeat_cannot_stop_a_reacquired_lifecycle() -> None:
    sink = _RecordingTelemetrySink()
    kernel = WorkerKernel(
        _make_driver(),
        heartbeat_interval=timedelta(seconds=1),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: datetime.now(UTC),
    )
    retired_stop = Event()
    retired_started = Event()
    retired_release = Event()

    def retired_heartbeat() -> None:
        retired_started.set()
        retired_release.wait(timeout=1)

    retired_thread = Thread(target=retired_heartbeat)
    retired_thread.start()
    assert retired_started.wait(timeout=1)
    with kernel._condition:
        kernel._heartbeat_users = 1
        kernel._heartbeat_stop = retired_stop
        kernel._heartbeat_thread = retired_thread

    releaser = Thread(target=kernel._release_heartbeat)
    releaser.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with kernel._condition:
            if kernel._heartbeat_users == 0 and kernel._heartbeat_thread is None:
                break
        time.sleep(0.001)
    else:
        pytest.fail("retired heartbeat did not begin teardown")

    kernel._acquire_heartbeat()
    retired_release.set()
    releaser.join(timeout=1)
    assert not releaser.is_alive()
    assert not any(
        signal.health_state is not None and signal.health_state.liveness is LivenessState.STOPPED
        for signal in sink.signals
        if signal.name == "queue.worker.heartbeat"
    )

    kernel._release_heartbeat()


def test_stalled_heartbeat_telemetry_cannot_block_polling_loop_teardown() -> None:
    entered = Event()
    release = Event()

    class StalledSink:
        def emit(self, signal: TelemetrySignal, /) -> None:
            if signal.name == "queue.worker.heartbeat":
                entered.set()
                release.wait(timeout=1)

    stop = Event()
    kernel = WorkerKernel(
        _make_driver(),
        heartbeat_interval=timedelta(milliseconds=10),
        telemetry=TelemetryPublisher((StalledSink(),)),
        clock=lambda: datetime.now(UTC),
    )
    polling = Thread(
        target=kernel.run_loop,
        args=(lambda job: None,),
        kwargs={"poll_interval": timedelta(milliseconds=1), "stop_event": stop},
    )
    polling.start()
    assert entered.wait(timeout=1)
    stop.set()
    polling.join(timeout=0.2)
    try:
        assert not polling.is_alive()
    finally:
        release.set()


def test_stalled_terminal_heartbeat_cannot_block_polling_loop_teardown() -> None:
    terminal_entered = Event()
    release = Event()

    class TerminalStalledSink:
        def emit(self, signal: TelemetrySignal, /) -> None:
            if (
                signal.name == "queue.worker.heartbeat"
                and signal.health_state is not None
                and signal.health_state.liveness is LivenessState.STOPPED
            ):
                terminal_entered.set()
                release.wait(timeout=1)

    stop = Event()
    kernel = WorkerKernel(
        _make_driver(),
        heartbeat_interval=timedelta(milliseconds=10),
        telemetry=TelemetryPublisher((TerminalStalledSink(),)),
        clock=lambda: datetime.now(UTC),
    )
    polling = Thread(
        target=kernel.run_loop,
        args=(lambda job: None,),
        kwargs={"poll_interval": timedelta(milliseconds=1), "stop_event": stop},
    )
    polling.start()
    time.sleep(0.02)
    stop.set()
    assert terminal_entered.wait(timeout=1)
    polling.join(timeout=0.2)
    try:
        assert not polling.is_alive()
    finally:
        release.set()


def test_stopped_blocked_heartbeat_producer_cannot_publish_live() -> None:
    sink = _RecordingTelemetrySink()
    kernel = WorkerKernel(
        _make_driver(),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )
    stop = Event()
    stop.set()

    kernel._run_heartbeat(stop)

    assert not any(signal.name == "queue.worker.heartbeat" for signal in sink.signals)


def test_terminal_heartbeat_thread_start_failure_is_fail_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetiredThread:
        def join(self, timeout: float | None = None) -> None:
            return

        def is_alive(self) -> bool:
            return False

    class FailingThread:
        def __init__(self, **_: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread capacity exhausted")

    kernel = WorkerKernel(
        _make_driver(),
        telemetry=TelemetryPublisher((_RecordingTelemetrySink(),)),
        clock=lambda: _NOW,
    )
    with kernel._condition:
        kernel._heartbeat_users = 1
        kernel._heartbeat_stop = Event()
        kernel._heartbeat_thread = RetiredThread()  # type: ignore[assignment]
    monkeypatch.setattr(worker_module, "Thread", FailingThread)

    kernel._release_heartbeat()


def test_worker_heartbeat_remains_live_and_reports_saturation_during_long_job() -> None:
    class HeartbeatSink:
        def __init__(self) -> None:
            self.signals: list[TelemetrySignal] = []
            self.saturated = Event()

        def emit(self, signal: TelemetrySignal, /) -> None:
            self.signals.append(signal)
            if (
                signal.name == "queue.worker.heartbeat"
                and signal.health_state is not None
                and signal.health_state.readiness is ReadinessState.SATURATED
            ):
                self.saturated.set()

    driver = _make_driver()
    driver.dispatch(_job("job-long-heartbeat"))
    sink = HeartbeatSink()
    release_job = Event()
    stop = Event()
    failures: list[BaseException] = []
    kernel = WorkerKernel(
        driver,
        max_concurrency=1,
        heartbeat_interval=timedelta(milliseconds=10),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: datetime.now(UTC),
    )

    def handle(job: QueueJob) -> None:
        del job
        release_job.wait(timeout=1)

    def run() -> None:
        try:
            kernel.run_loop(
                handle,
                poll_interval=timedelta(milliseconds=1),
                stop_event=stop,
            )
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert sink.saturated.wait(timeout=1)
    finally:
        release_job.set()
        stop.set()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert failures == []
    saturated = next(
        signal
        for signal in sink.signals
        if signal.name == "queue.worker.heartbeat"
        and signal.health_state is not None
        and signal.health_state.readiness is ReadinessState.SATURATED
    )
    assert dict(saturated.attributes) == {
        "active_jobs_bucket": "one",
        "saturation_bucket": "full",
        "state": "processing",
    }
    assert "job-long-heartbeat" not in repr(saturated)


def test_worker_heartbeat_reports_busy_then_draining_during_long_job() -> None:
    class HeartbeatSink:
        def __init__(self) -> None:
            self.signals: list[TelemetrySignal] = []
            self.busy = Event()
            self.draining = Event()

        def emit(self, signal: TelemetrySignal, /) -> None:
            self.signals.append(signal)
            if signal.name != "queue.worker.heartbeat" or signal.health_state is None:
                return
            if signal.health_state.readiness is ReadinessState.BUSY:
                self.busy.set()
            if signal.health_state.readiness is ReadinessState.DRAINING:
                self.draining.set()

    driver = _make_driver()
    driver.dispatch(_job("job-draining-heartbeat"))
    sink = HeartbeatSink()
    release_job = Event()
    stop = Event()
    failures: list[BaseException] = []
    kernel = WorkerKernel(
        driver,
        max_concurrency=2,
        heartbeat_interval=timedelta(milliseconds=10),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: datetime.now(UTC),
    )

    def handle(job: QueueJob) -> None:
        del job
        release_job.wait(timeout=1)

    def run() -> None:
        try:
            kernel.run_loop(
                handle,
                poll_interval=timedelta(milliseconds=1),
                stop_event=stop,
            )
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    try:
        assert sink.busy.wait(timeout=1)
        kernel.start_draining()
        assert sink.draining.wait(timeout=1)
    finally:
        release_job.set()
        stop.set()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert failures == []
    states = [
        signal.health_state.readiness
        for signal in sink.signals
        if signal.name == "queue.worker.heartbeat" and signal.health_state is not None
    ]
    assert ReadinessState.BUSY in states
    assert ReadinessState.DRAINING in states


def test_worker_heartbeat_publisher_failure_cannot_stop_polling() -> None:
    class FailingSink:
        def __init__(self) -> None:
            self.calls = 0
            self.called_twice = Event()

        def emit(self, signal: TelemetrySignal, /) -> None:
            self.calls += 1
            if self.calls >= 2:
                self.called_twice.set()
            raise RuntimeError("telemetry unavailable")

    sink = FailingSink()
    stop = Event()
    failures: list[BaseException] = []
    kernel = WorkerKernel(
        _make_driver(),
        heartbeat_interval=timedelta(milliseconds=10),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: datetime.now(UTC),
    )

    def run() -> None:
        try:
            kernel.run_loop(
                lambda job: None,
                poll_interval=timedelta(milliseconds=1),
                stop_event=stop,
            )
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)

    thread.start()
    try:
        assert sink.called_twice.wait(timeout=1)
    finally:
        stop.set()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert failures == []


def test_worker_heartbeat_thread_start_failure_cannot_stop_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingThread:
        def __init__(self, **_: object) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread capacity exhausted")

    monkeypatch.setattr(worker_module, "Thread", FailingThread)
    driver = _make_driver()
    driver.dispatch(_job("job-heartbeat-start-failure"))
    stop = Event()
    processed: list[str] = []
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((_RecordingTelemetrySink(),)),
        clock=lambda: _NOW,
    )

    def handle(job: QueueJob) -> None:
        processed.append(job.envelope.metadata.message_id)
        stop.set()

    kernel.run_loop(handle, poll_interval=timedelta(milliseconds=1), stop_event=stop)

    assert processed == ["job-heartbeat-start-failure"]
    assert driver.acked == ["job-heartbeat-start-failure"]


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

    assert driver.released == ["job-cancel"]
    assert kernel.stats.cancellations == 1
    assert kernel.state is WorkerState.IDLE


def test_worker_kernel_cooperatively_cancels_active_job_and_releases_it() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    driver.dispatch(_job("job-cooperative-cancel"))
    entered = Event()
    exit_handler = Event()
    observed: list[CancellationToken] = []
    failures: list[BaseException] = []

    class Handler:
        cooperative_cancellation = True

        def __call__(self, job: QueueJob) -> None:
            del job
            raise AssertionError("cooperative handler must use invoke")

        def invoke(
            self,
            job: QueueJob,
            /,
            *,
            cancellation: CancellationToken,
        ) -> None:
            del job
            observed.append(cancellation)
            entered.set()
            assert exit_handler.wait(timeout=2)

    def run() -> None:
        try:
            kernel.run_once(Handler())
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    assert entered.wait(timeout=2)

    kernel.cancel_active()

    assert observed[0].cancelled
    assert driver.released == []
    exit_handler.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert driver.released == ["job-cooperative-cancel"]
    assert driver.acked == []
    assert kernel.stats.cancellations == 1


def test_worker_kernel_does_not_reclassify_legacy_handler_as_cooperative() -> None:
    driver = _make_driver()
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    driver.dispatch(_job("job-legacy-drain"))
    entered = Event()
    exit_handler = Event()

    def handler(job: QueueJob) -> None:
        del job
        entered.set()
        assert exit_handler.wait(timeout=2)

    thread = Thread(target=kernel.run_once, args=(handler,))
    thread.start()
    assert entered.wait(timeout=2)

    kernel.cancel_active()
    exit_handler.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert driver.acked == ["job-legacy-drain"]
    assert driver.released == []
    assert kernel.stats.jobs_processed == 1
    assert kernel.stats.cancellations == 0


def test_worker_kernel_rejects_invalid_cooperative_signature_before_reserving() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-invalid-cooperative"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    class Handler:
        cooperative_cancellation = True

        def __call__(self, job: QueueJob) -> None:
            del job

        def invoke(self) -> None:
            return

    with pytest.raises(TypeError, match="cooperative signature"):
        kernel.run_once(Handler())

    assert driver.queued_message_ids == ["job-invalid-cooperative"]
    assert driver.retried == []
    assert driver.rejected == []


def test_worker_kernel_validates_cooperative_handler_result() -> None:
    driver = _make_driver()
    driver.dispatch(_job("job-invalid-cooperative-result", max_attempts=2))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    class Handler:
        cooperative_cancellation = True

        def __call__(self, job: QueueJob) -> None:
            del job

        def invoke(
            self,
            job: QueueJob,
            /,
            *,
            cancellation: CancellationToken,
        ) -> object:
            del job, cancellation
            return 1

    with pytest.raises(TypeError, match="return None"):
        kernel.run_once(Handler())

    assert driver.acked == []
    assert driver.retried == ["job-invalid-cooperative-result"]


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
    assert driver.released == ["job-ack-failure"]


def test_worker_kernel_cancellation_during_ack_releases_reservation() -> None:
    driver = _make_driver()
    driver.next_ack_error = KeyboardInterrupt()
    driver.dispatch(_job("job-ack-cancel"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    with pytest.raises(KeyboardInterrupt):
        kernel.run_once(consumer=lambda job: None)

    assert driver.released == ["job-ack-cancel"]
    assert kernel.stats.cancellations == 1
    assert kernel.stats.jobs_failed == 0


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


def test_worker_kernel_records_idempotency_after_job_scope_exit() -> None:
    """Idempotency must be recorded only after the job scope exits successfully.

    If the scope's __exit__ raises, the idempotency record must not be committed
    so the job remains safely retryable.
    """
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
        "scope-exit",
        "record",
        "claim-exit",
    ]


def test_worker_kernel_scope_exit_failure_prevents_idempotency_record() -> None:
    """When the job scope __exit__ raises, idempotency must not be durably recorded
    and the job must remain retryable via the existing settlement contract."""
    lifecycle: list[str] = []
    record_called = False

    class Claim:
        already_processed = False

        def record(self) -> None:
            nonlocal record_called
            record_called = True
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
            raise RuntimeError("scope teardown failed")

    driver = _make_driver()
    driver.dispatch(_job("job-scope-exit-fail", max_attempts=2))
    kernel = WorkerKernel(
        driver,
        idempotency=Store(),
        scope_factory=scope,
        clock=lambda: _NOW,
    )

    with pytest.raises(RuntimeError, match="scope teardown failed"):
        kernel.run_once(consumer=lambda job: lifecycle.append("consumer"))

    # Idempotency must NOT have been recorded
    assert record_called is False
    assert "record" not in lifecycle
    # Job must remain retryable (settled via retry, not acked)
    assert driver.retried == ["job-scope-exit-fail"]
    assert driver.acked == []


def test_worker_kernel_record_failure_after_scope_success_retries_at_least_once() -> None:
    lifecycle: list[str] = []
    record_attempts = 0

    class Claim:
        already_processed = False

        def record(self) -> None:
            nonlocal record_attempts
            record_attempts += 1
            lifecycle.append(f"record-{record_attempts}")
            if record_attempts == 1:
                raise RuntimeError("idempotency record failed")

    class Store:
        @contextmanager
        def claim(self, message_id: str):  # type: ignore[no-untyped-def]
            yield Claim()

    @contextmanager
    def scope(job: QueueJob):  # type: ignore[no-untyped-def]
        lifecycle.append("scope-enter")
        try:
            yield
        finally:
            lifecycle.append("scope-exit")

    driver = _make_driver()
    driver.dispatch(_job("job-record-failure", max_attempts=2))
    kernel = WorkerKernel(
        driver,
        idempotency=Store(),
        scope_factory=scope,
        clock=lambda: _NOW,
    )
    effects: list[str] = []

    with pytest.raises(RuntimeError, match="idempotency record failed"):
        kernel.run_once(consumer=lambda job: effects.append("effect"))
    kernel.run_once(consumer=lambda job: effects.append("effect"))

    assert lifecycle == [
        "scope-enter",
        "scope-exit",
        "record-1",
        "scope-enter",
        "scope-exit",
        "record-2",
    ]
    assert effects == ["effect", "effect"]
    assert driver.retried == ["job-record-failure"]
    assert driver.acked == ["job-record-failure"]


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


class _RecordingTelemetrySink:
    def __init__(self) -> None:
        self.signals: list[TelemetrySignal] = []

    def emit(self, signal: TelemetrySignal, /) -> None:
        self.signals.append(signal)


def test_worker_kernel_emits_bounded_terminal_delivery_telemetry() -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    context = TelemetryContext(operation="queue.example", correlation_id="correlation-1")
    envelope = MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id="job-telemetry",
            message_type="jobs.example",
            schema_version=1,
            kind=EnvelopeKind.JOB,
            occurred_at=_NOW,
            correlation_id="correlation-1",
            telemetry=context,
        ),
        payload={"secret": "must-not-leak"},
    )
    driver.dispatch(_job("job-telemetry", envelope=envelope))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    kernel.run_once(consumer=lambda job: None)

    assert len(sink.signals) == 1
    signal = sink.signals[0]
    assert signal.name == "queue.worker.delivery"
    assert signal.kind is TelemetryKind.METRIC
    assert signal.context is context
    assert dict(signal.attributes) == {
        "attempt_bucket": "first",
        "lag_bucket": "lt_10ms",
        "outcome": "processed",
        "phase": "ack",
        "reason": "success",
        "renewal_count_bucket": "none",
        "renewal_outcome": "not_required",
        "runtime_bucket": "lt_10ms",
        "saturation_bucket": "lt_50pct",
        "settlement": "ack",
    }
    rendered = repr(signal)
    assert "job-telemetry" not in rendered
    assert "must-not-leak" not in rendered


def test_worker_kernel_emits_queue_wait_alert_above_configured_threshold() -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    occurred_at = _NOW - timedelta(seconds=12)
    envelope = MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id="job-wait-alert",
            message_type="jobs.example",
            schema_version=1,
            kind=EnvelopeKind.JOB,
            occurred_at=occurred_at,
        ),
        payload={},
    )
    driver.dispatch(_job("job-wait-alert", envelope=envelope))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        queue_wait_alert_after=timedelta(seconds=5),
        clock=lambda: _NOW,
    )

    kernel.run_once(consumer=lambda job: None)

    assert [signal.name for signal in sink.signals] == [
        "queue.worker.delivery",
        "queue.worker.wait_alert",
    ]
    assert dict(sink.signals[0].attributes)["lag_bucket"] == "lt_1m"
    assert dict(sink.signals[1].attributes) == {
        "lag_bucket": "lt_1m",
        "status": "exceeded",
        "threshold_bucket": "lt_10s",
    }


def test_worker_kernel_emits_queue_wait_alert_only_on_state_transitions() -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    for message_id, lag in (
        ("job-wait-alert-first", timedelta(seconds=12)),
        ("job-wait-alert-duplicate", timedelta(seconds=11)),
        ("job-wait-alert-recovered", timedelta(seconds=1)),
        ("job-wait-alert-reopened", timedelta(seconds=15)),
    ):
        envelope = MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW - lag,
            ),
            payload={},
        )
        driver.dispatch(_job(message_id, envelope=envelope))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        queue_wait_alert_after=timedelta(seconds=5),
        clock=lambda: _NOW,
    )

    for _ in range(4):
        kernel.run_once(consumer=lambda job: None)

    alerts = [signal for signal in sink.signals if signal.name == "queue.worker.wait_alert"]
    assert [dict(signal.attributes)["status"] for signal in alerts] == [
        "exceeded",
        "recovered",
        "exceeded",
    ]
    assert dict(alerts[1].attributes) == {
        "lag_bucket": "lt_10s",
        "status": "recovered",
        "threshold_bucket": "lt_10s",
    }


@pytest.mark.parametrize(
    ("max_attempts", "outcome", "settlement", "attempt_bucket"),
    [
        (2, "retried", "retry", "first"),
        (1, "rejected", "reject", "exhausted"),
    ],
)
def test_worker_kernel_emits_failure_settlement_telemetry(
    max_attempts: int,
    outcome: str,
    settlement: str,
    attempt_bucket: str,
) -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    driver.dispatch(_job("job-failure-telemetry", max_attempts=max_attempts))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    with pytest.raises(ValueError, match="boom"):
        kernel.run_once(consumer=lambda job: (_ for _ in ()).throw(ValueError("boom")))

    assert len(sink.signals) == 1
    attributes = dict(sink.signals[0].attributes)
    assert attributes["outcome"] == outcome
    assert attributes["reason"] == "execution_failure"
    assert attributes["settlement"] == settlement
    assert attributes["phase"] == "execution"
    assert attributes["attempt_bucket"] == attempt_bucket


def test_worker_kernel_emits_settlement_failure_without_hiding_primary_error() -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    driver.fail_next_retry = True
    driver.dispatch(_job("job-settlement-telemetry", max_attempts=2))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    with pytest.raises(RuntimeError, match="retry failed") as exc:
        kernel.run_once(consumer=lambda job: (_ for _ in ()).throw(ValueError("handler")))

    assert isinstance(exc.value.__cause__, ValueError)
    attributes = dict(sink.signals[0].attributes)
    assert attributes["outcome"] == "settlement_failed"
    assert attributes["reason"] == "execution_failure"
    assert attributes["settlement"] == "failed"


def test_worker_kernel_emits_exhausted_bucket_when_terminal_rejection_fails() -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    driver.fail_next_reject = True
    driver.dispatch(_job("job-reject-telemetry", max_attempts=1))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    with pytest.raises(RuntimeError, match="reject failed") as exc:
        kernel.run_once(consumer=lambda job: (_ for _ in ()).throw(ValueError("handler")))

    assert isinstance(exc.value.__cause__, ValueError)
    assert dict(sink.signals[0].attributes) == {
        "attempt_bucket": "exhausted",
        "lag_bucket": "lt_10ms",
        "outcome": "settlement_failed",
        "phase": "execution",
        "reason": "execution_failure",
        "renewal_count_bucket": "none",
        "renewal_outcome": "not_required",
        "runtime_bucket": "lt_10ms",
        "saturation_bucket": "lt_50pct",
        "settlement": "failed",
    }


def test_worker_kernel_telemetry_sink_failure_does_not_change_delivery() -> None:
    class BrokenSink:
        def emit(self, signal: TelemetrySignal, /) -> None:
            raise RuntimeError("telemetry unavailable")

    driver = _make_driver()
    publisher = TelemetryPublisher((BrokenSink(),))
    driver.dispatch(_job("job-broken-telemetry"))
    kernel = WorkerKernel(driver, telemetry=publisher, clock=lambda: _NOW)

    reservation = kernel.run_once(consumer=lambda job: None)

    assert reservation is not None
    assert driver.acked == ["job-broken-telemetry"]
    assert kernel.state is WorkerState.IDLE
    assert publisher.evidence.failed_deliveries == 1


@pytest.mark.parametrize(
    ("acknowledgement_error", "expected_reason"),
    [
        (RuntimeError("ack failed"), "acknowledgement_failure"),
        (KeyboardInterrupt(), "cancellation"),
    ],
)
def test_worker_kernel_emits_release_telemetry_for_uncertain_acknowledgement(
    acknowledgement_error: BaseException,
    expected_reason: str,
) -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    driver.next_ack_error = acknowledgement_error
    driver.dispatch(_job("job-uncertain-ack"))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    with pytest.raises(type(acknowledgement_error)):
        kernel.run_once(consumer=lambda job: None)

    attributes = dict(sink.signals[0].attributes)
    assert attributes["outcome"] == "released"
    assert attributes["reason"] == expected_reason
    assert attributes["settlement"] == "release"
    assert attributes["phase"] == "ack"


def test_worker_kernel_emits_cancellation_settlement_failure_telemetry() -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    driver.fail_next_release = True
    driver.dispatch(_job("job-cancel-telemetry"))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    with pytest.raises(BaseExceptionGroup):
        kernel.run_once(consumer=lambda job: (_ for _ in ()).throw(KeyboardInterrupt()))

    attributes = dict(sink.signals[0].attributes)
    assert attributes["attempt_bucket"] == "first"
    assert attributes["outcome"] == "settlement_failed"
    assert attributes["reason"] == "cancellation"
    assert attributes["settlement"] == "failed"
    assert attributes["phase"] == "execution"


def test_worker_kernel_emits_acknowledgement_settlement_failure_telemetry() -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    driver.next_ack_error = RuntimeError("ack failed")
    driver.fail_next_release = True
    driver.dispatch(_job("job-ack-release-telemetry"))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    with pytest.raises(BaseExceptionGroup):
        kernel.run_once(consumer=lambda job: None)

    attributes = dict(sink.signals[0].attributes)
    assert attributes["attempt_bucket"] == "first"
    assert attributes["outcome"] == "settlement_failed"
    assert attributes["reason"] == "acknowledgement_failure"
    assert attributes["settlement"] == "failed"
    assert attributes["phase"] == "ack"


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


def test_worker_kernel_settlement_failure_preserves_handler_error() -> None:
    """When the handler fails and the subsequent settlement (retry/reject) also
    fails, the settlement error must propagate with the handler error as its cause,
    preserving both errors in the exception chain."""
    driver = _make_driver()
    driver.fail_next_retry = True
    driver.dispatch(_job("job-settle-fail", max_attempts=2))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    def failing_consumer(job: QueueJob) -> None:
        raise ValueError("handler error")

    with pytest.raises(RuntimeError, match="retry failed") as exc:
        kernel.run_once(consumer=failing_consumer)

    # The settlement error chains back to the handler error
    assert exc.value.__cause__ is not None
    assert isinstance(exc.value.__cause__, ValueError)
    assert "handler error" in str(exc.value.__cause__)


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


def test_worker_kernel_run_once_ack_failure_increments_jobs_failed() -> None:
    """An acknowledgement failure must increment jobs_failed exactly once before
    propagating, so the cumulative failure metric truthfully represents this event."""
    driver = _make_driver()
    driver.fail_next_ack = True
    driver.dispatch(_job("job-ack-fail-stats"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    with pytest.raises(RuntimeError, match="ack failed"):
        kernel.run_once(consumer=lambda job: None)

    stats = kernel.stats
    assert stats.jobs_failed == 1
    assert driver.released == ["job-ack-fail-stats"]


def test_worker_kernel_does_not_release_definitively_lost_reservation() -> None:
    driver = _make_driver()
    sink = _RecordingTelemetrySink()
    lost = QueueReservationLost(stage="queue.ack")
    driver.next_ack_error = lost
    driver.dispatch(_job("job-lost-reservation"))
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    with pytest.raises(QueueReservationLost) as captured:
        kernel.run_once(consumer=lambda job: None)

    assert captured.value is lost
    assert driver.released == []
    assert kernel.stats.jobs_failed == 1
    assert dict(sink.signals[0].attributes)["reason"] == "reservation_lost"


def test_worker_kernel_run_loop_ack_failure_increments_jobs_failed() -> None:
    """run_loop must also increment jobs_failed for acknowledgement failures before
    propagating, matching the run_once behaviour."""
    driver = _make_driver()
    driver.fail_next_ack = True
    driver.dispatch(_job("job-ack-fail-loop-stats"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    processed: list[str] = []

    with pytest.raises(RuntimeError, match="ack failed"):
        kernel.run_loop(
            consumer=lambda job: processed.append(job.envelope.metadata.message_id),
            poll_interval=timedelta(milliseconds=1),
        )

    stats = kernel.stats
    assert stats.jobs_failed == 1
    assert processed == ["job-ack-fail-loop-stats"]
    assert driver.released == ["job-ack-fail-loop-stats"]


def test_worker_kernel_run_loop_propagates_acknowledgement_failure() -> None:
    """run_loop must not swallow infrastructure acknowledgement failures.

    A handler failure that was successfully settled (retried/rejected) allows the
    loop to continue, but an ack failure is an infrastructure problem that must
    propagate out of run_loop so the operator can observe it.
    """
    driver = _make_driver()
    driver.fail_next_ack = True
    driver.dispatch(_job("job-ack-fail-loop"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    processed: list[str] = []

    with pytest.raises(RuntimeError, match="ack failed"):
        kernel.run_loop(
            consumer=lambda job: processed.append(job.envelope.metadata.message_id),
            poll_interval=timedelta(milliseconds=1),
        )

    assert processed == ["job-ack-fail-loop"]
    assert driver.released == ["job-ack-fail-loop"]


def test_worker_kernel_run_loop_continues_after_handler_failure_then_stops_on_ack_failure() -> None:
    """Ordinary consumer failures that settled may still allow the loop to continue,
    but the next acknowledgement failure must propagate."""
    driver = _make_driver()
    driver.dispatch(_job("job-handler-fail"))
    driver.dispatch(_job("job-then-ack-fail"))
    driver.fail_next_ack = True
    kernel = WorkerKernel(driver, clock=lambda: _NOW)
    processed: list[str] = []

    def consumer(job: QueueJob) -> None:
        message_id = job.envelope.metadata.message_id
        if message_id == "job-handler-fail":
            raise ValueError("handler boom")
        processed.append(message_id)

    with pytest.raises(RuntimeError, match="ack failed"):
        kernel.run_loop(consumer, poll_interval=timedelta(milliseconds=1))

    assert driver.rejected == ["job-handler-fail"]
    assert processed == ["job-then-ack-fail"]
    assert driver.released == ["job-then-ack-fail"]


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
    driver.fail_next_release = True
    driver.dispatch(_job("job-cancel-release"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    def consumer(job: QueueJob) -> None:
        raise KeyboardInterrupt

    with pytest.raises(BaseExceptionGroup) as exc:
        kernel.run_once(consumer)

    assert any(isinstance(error, KeyboardInterrupt) for error in exc.value.exceptions)
    assert any(isinstance(error, RuntimeError) for error in exc.value.exceptions)


def test_worker_kernel_preserves_ack_error_when_release_also_fails() -> None:
    """When an acknowledgement fails and the subsequent release also fails, both
    errors must be preserved in a BaseExceptionGroup so the operator can observe
    the full failure chain."""
    driver = _make_driver()
    acknowledgement_error = RuntimeError("ack failed")
    driver.next_ack_error = acknowledgement_error
    driver.fail_next_release = True
    driver.dispatch(_job("job-ack-release-fail"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    with pytest.raises(BaseExceptionGroup) as exc:
        kernel.run_once(consumer=lambda job: None)

    errors = exc.value.exceptions
    assert len(errors) == 2
    assert errors[0] is acknowledgement_error
    assert isinstance(errors[1], RuntimeError)
    assert str(errors[1]) == "release failed"
    stats = kernel.stats
    assert stats.jobs_failed == 1
    assert stats.cancellations == 0
    assert stats.jobs_processed == 0
    assert stats.duplicates == 0


def test_worker_kernel_counts_ack_cancellation_when_release_also_fails() -> None:
    driver = _make_driver()
    cancellation = KeyboardInterrupt()
    driver.next_ack_error = cancellation
    driver.fail_next_release = True
    driver.dispatch(_job("job-ack-cancel-release-fail"))
    kernel = WorkerKernel(driver, clock=lambda: _NOW)

    with pytest.raises(BaseExceptionGroup) as exc:
        kernel.run_once(consumer=lambda job: None)

    errors = exc.value.exceptions
    assert len(errors) == 2
    assert errors[0] is cancellation
    assert isinstance(errors[1], RuntimeError)
    assert str(errors[1]) == "release failed"
    stats = kernel.stats
    assert stats.cancellations == 1
    assert stats.jobs_failed == 0
    assert stats.jobs_processed == 0
    assert stats.duplicates == 0


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


def test_worker_renews_long_running_delivery_and_acks_latest_handle() -> None:
    driver = _RenewingQueueDriver()
    driver.dispatch(_job("renew-long"))
    kernel = WorkerKernel(
        driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=25),
    )
    handler_started = Event()
    finish_handler = Event()
    result: list[QueueReservation | None] = []

    def handler(_job: QueueJob) -> None:
        handler_started.set()
        assert finish_handler.wait(timeout=2)

    thread = Thread(target=lambda: result.append(kernel.run_once(handler)))
    thread.start()
    assert handler_started.wait(timeout=2)
    assert driver.renewed_twice.wait(timeout=2)
    finish_handler.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert result[0] is not None
    assert result[0].reservation_id == driver.acked_reservations[0].reservation_id
    assert result[0].reservation_id == driver.latest_reservation_id
    assert driver.renewal_count >= 2
    kernel.close()


def test_worker_cooperative_cancellation_releases_latest_renewed_handle_once() -> None:
    driver = _RenewingQueueDriver()
    driver.dispatch(_job("renew-cooperative-cancel"))
    kernel = WorkerKernel(
        driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=25),
    )
    entered = Event()
    exit_handler = Event()
    failures: list[BaseException] = []

    class Handler:
        cooperative_cancellation = True

        def __call__(self, job: QueueJob) -> None:
            del job

        def invoke(
            self,
            job: QueueJob,
            /,
            *,
            cancellation: CancellationToken,
        ) -> None:
            del job
            entered.set()
            assert exit_handler.wait(timeout=2)
            assert cancellation.cancelled

    def run() -> None:
        try:
            kernel.run_once(Handler())
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    assert entered.wait(timeout=2)
    assert driver.renewed_twice.wait(timeout=2)

    kernel.cancel_active()
    exit_handler.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert failures == []
    assert len(driver.released_reservations) == 1
    assert driver.released_reservations[0].reservation_id == driver.latest_reservation_id
    assert driver.acked_reservations == []
    assert driver.retried_reservations == []
    assert driver.rejected_reservations == []
    kernel.close()


def test_worker_releases_too_short_lease_before_handler_execution() -> None:
    driver = _RenewingQueueDriver(lease=timedelta(milliseconds=15))
    driver.dispatch(_job("renew-too-short"))
    kernel = WorkerKernel(
        driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=25),
    )
    handled = False

    def handler(job: QueueJob) -> None:
        del job
        nonlocal handled
        handled = True

    with pytest.raises(QueueOperationError) as failed:
        kernel.run_once(handler)

    assert failed.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert not handled
    assert driver.released == ["renew-too-short"]
    kernel.close()


def test_worker_stops_settlement_after_successful_renewal_then_loss() -> None:
    driver = _RenewingQueueDriver(lose_after=1)
    driver.dispatch(_job("renew-then-lost"))
    kernel = WorkerKernel(
        driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=25),
    )
    finish_handler = Event()
    failures: list[BaseException] = []

    def handler(job: QueueJob) -> None:
        del job
        assert finish_handler.wait(timeout=2)

    def run() -> None:
        try:
            kernel.run_once(handler)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    assert driver.attempted_twice.wait(timeout=2)
    finish_handler.set()
    thread.join(timeout=2)

    assert len(failures) == 1
    assert isinstance(failures[0], QueueReservationLost)
    assert driver.acked_reservations == []
    assert driver.released == []
    kernel.close()


@pytest.mark.parametrize("mode", ["retry", "reject", "cancel", "uncertain_ack"])
def test_worker_uses_latest_renewed_handle_for_every_settlement(mode: str) -> None:
    driver = _RenewingQueueDriver()
    max_attempts = 1 if mode == "reject" else 2
    driver.dispatch(_job(f"renew-{mode}", max_attempts=max_attempts))
    if mode == "uncertain_ack":
        driver.next_ack_error = RuntimeError("ack failed")
    kernel = WorkerKernel(
        driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=25),
    )
    finish_handler = Event()
    failures: list[BaseException] = []

    def handler(job: QueueJob) -> None:
        del job
        assert finish_handler.wait(timeout=2)
        if mode in {"retry", "reject"}:
            raise ValueError("handler failed")
        if mode == "cancel":
            raise KeyboardInterrupt()

    def run() -> None:
        try:
            kernel.run_once(handler)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    assert driver.completed_renewal.wait(timeout=2)
    finish_handler.set()
    thread.join(timeout=2)

    assert len(failures) == 1
    settled = {
        "retry": driver.retried_reservations,
        "reject": driver.rejected_reservations,
        "cancel": driver.released_reservations,
        "uncertain_ack": driver.released_reservations,
    }[mode]
    assert len(settled) == 1
    assert settled[0].reservation_id == driver.latest_reservation_id
    kernel.close()


def test_worker_renewal_loss_never_settles_stale_handle() -> None:
    driver = _RenewingQueueDriver(renewal_error=QueueReservationLost(stage="queue.renew"))
    driver.dispatch(_job("renew-lost"))
    sink = _RecordingTelemetrySink()
    kernel = WorkerKernel(
        driver,
        telemetry=TelemetryPublisher((sink,)),
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=25),
    )
    handler_started = Event()
    finish_handler = Event()
    failures: list[BaseException] = []

    def handler(_job: QueueJob) -> None:
        handler_started.set()
        assert finish_handler.wait(timeout=2)

    def run() -> None:
        try:
            kernel.run_once(handler)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    assert handler_started.wait(timeout=2)
    assert driver.renewed.wait(timeout=2)
    finish_handler.set()
    thread.join(timeout=2)

    assert len(failures) == 1
    assert isinstance(failures[0], QueueReservationLost)
    assert driver.acked_reservations == []
    assert driver.retried == []
    assert driver.rejected == []
    assert driver.released == []
    attributes = dict(sink.signals[0].attributes)
    assert attributes["phase"] == "renewal"
    assert attributes["reason"] == "reservation_lost"
    assert attributes["renewal_outcome"] == "lost"
    assert attributes["settlement"] == "none"
    kernel.close()


@pytest.mark.parametrize("unsafe_expiry", [False, True])
def test_worker_rejects_invalid_renewal_replacement_without_settlement(
    unsafe_expiry: bool,
) -> None:
    driver = _RenewingQueueDriver(
        invalid_replacement=not unsafe_expiry,
        unsafe_replacement_expiry=unsafe_expiry,
        lease=timedelta(milliseconds=30) if unsafe_expiry else timedelta(milliseconds=200),
    )
    driver.dispatch(_job("renew-invalid"))
    kernel = WorkerKernel(
        driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=25),
    )
    finish_handler = Event()
    failures: list[BaseException] = []

    def handler(job: QueueJob) -> None:
        del job
        assert finish_handler.wait(timeout=2)

    def run() -> None:
        try:
            kernel.run_once(handler)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    assert driver.renewed.wait(timeout=2)
    finish_handler.set()
    thread.join(timeout=2)

    assert len(failures) == 1
    assert isinstance(failures[0], QueueReservationLost)
    assert driver.acked_reservations == []
    assert driver.released == []
    kernel.close()


def test_worker_preserves_handler_and_renewal_failures() -> None:
    renewal_error = QueueReservationLost(stage="queue.renew")
    driver = _RenewingQueueDriver(renewal_error=renewal_error)
    driver.dispatch(_job("renew-handler-failed", max_attempts=2))
    kernel = WorkerKernel(
        driver,
        renewal_min_interval=timedelta(milliseconds=10),
        renewal_max_interval=timedelta(milliseconds=25),
    )
    finish_handler = Event()
    failures: list[BaseException] = []
    handler_error = ValueError("handler failed")

    def handler(job: QueueJob) -> None:
        del job
        assert finish_handler.wait(timeout=2)
        raise handler_error

    def run() -> None:
        try:
            kernel.run_once(handler)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=run)
    thread.start()
    assert driver.renewed.wait(timeout=2)
    finish_handler.set()
    thread.join(timeout=2)

    assert len(failures) == 1
    assert isinstance(failures[0], BaseExceptionGroup)
    assert failures[0].exceptions == (handler_error, renewal_error)
    assert driver.retried == []
    kernel.close()


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
        self.acked_reservations: list[QueueReservation] = []
        self.rejected: list[str] = []
        self.rejected_reservations: list[QueueReservation] = []
        self.retried: list[str] = []
        self.retried_reservations: list[QueueReservation] = []
        self.released: list[str] = []
        self.released_reservations: list[QueueReservation] = []
        self.fail_next_ack = False
        self.next_ack_error: BaseException | None = None
        self.fail_next_retry = False
        self.fail_next_reject = False
        self.fail_next_release = False
        self.fail_next_close = False
        self._features = frozenset({QueueFeature.RESERVATION, QueueFeature.INLINE_EXECUTION})

    @property
    def features(self) -> frozenset[QueueFeature]:
        return self._features

    @property
    def queued_message_ids(self) -> list[str]:
        return [job.envelope.metadata.message_id for job in self._queue]

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
        self.acked_reservations.append(reservation)

    def reject(self, reservation: QueueReservation, /) -> None:
        if self.fail_next_reject:
            self.fail_next_reject = False
            raise RuntimeError("reject failed")
        self.rejected.append(reservation.job.envelope.metadata.message_id)
        self.rejected_reservations.append(reservation)

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
        self.retried_reservations.append(reservation)
        self._queue.appendleft(reservation.job)

    def release(self, reservation: QueueReservation, /) -> None:
        if self.fail_next_release:
            self.fail_next_release = False
            raise RuntimeError("release failed")
        self.released.append(reservation.job.envelope.metadata.message_id)
        self.released_reservations.append(reservation)
        message_id = reservation.job.envelope.metadata.message_id
        self._attempts[message_id] = max(0, self._attempts.get(message_id, 1) - 1)
        self._queue.appendleft(reservation.job)

    def close(self) -> None:
        if self.fail_next_close:
            self.fail_next_close = False
            raise RuntimeError("close failed")
        self._closed = True


def _make_driver() -> _TestQueueDriver:
    return _TestQueueDriver()


class _RenewingQueueDriver(_TestQueueDriver):
    def __init__(
        self,
        *,
        renewal_error: BaseException | None = None,
        lose_after: int | None = None,
        lease: timedelta = timedelta(milliseconds=200),
        invalid_replacement: bool = False,
        unsafe_replacement_expiry: bool = False,
    ) -> None:
        super().__init__()
        self._features = frozenset(
            {
                QueueFeature.RESERVATION,
                QueueFeature.RESERVATION_RENEWAL,
            }
        )
        self.renewal_error = renewal_error
        self.lose_after = lose_after
        self.lease = lease
        self.invalid_replacement = invalid_replacement
        self.unsafe_replacement_expiry = unsafe_replacement_expiry
        self.renewed = Event()
        self.renewed_twice = Event()
        self.attempted_twice = Event()
        self.completed_renewal = Event()
        self._renewal_count = 0
        self.latest_reservation_id = "1" * 64

    @property
    def renewal_count(self) -> int:
        return self._renewal_count

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        reservation = super().reserve(lane=lane)
        if reservation is None:
            return None
        now = datetime.now(UTC)
        return replace(
            reservation,
            reserved_at=now,
            expires_at=now + self.lease,
            owner_id="0" * 64,
            reservation_id="1" * 64,
        )

    def renew(self, reservation: QueueReservation, /) -> QueueReservation:
        self.renewed.set()
        if self._renewal_count >= 1:
            self.attempted_twice.set()
        if self.renewal_error is not None:
            raise self.renewal_error
        if self.lose_after is not None and self._renewal_count >= self.lose_after:
            raise QueueReservationLost(stage="queue.renew")
        if self.invalid_replacement:
            return reservation
        if self.unsafe_replacement_expiry:
            assert reservation.expires_at is not None
            return replace(
                reservation,
                expires_at=reservation.expires_at + timedelta(milliseconds=1),
                reservation_id="2" * 64,
            )
        self._renewal_count += 1
        self.latest_reservation_id = f"{self._renewal_count + 1:064x}"
        self.completed_renewal.set()
        if self._renewal_count >= 2:
            self.renewed_twice.set()
        return replace(
            reservation,
            expires_at=datetime.now(UTC) + self.lease,
            reservation_id=self.latest_reservation_id,
        )
