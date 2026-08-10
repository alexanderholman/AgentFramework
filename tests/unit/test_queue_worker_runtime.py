from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread, get_ident
from time import monotonic
from typing import Any

import pytest

from af.framework.container import Container, InvocationKind, Scope
from af.framework.context import CancellationToken, current_context
from af.framework.errors import PlatformError, PlatformReason
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata, PolicyContext
from af.observability import TelemetryContext
from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
)
from af.queue.runtime import ContainerJobHandlerFactory, WorkerRuntime, WorkerRuntimeState
from af.queue.worker import WorkerKernel

_NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _job(message_id: str = "runtime-job") -> QueueJob:
    tenant_ref = f"sha256:{'1' * 64}"
    return QueueJob(
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.runtime",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW,
                correlation_id="correlation",
                causation_id="cause",
                deadline=_NOW + timedelta(minutes=1),
                policy=PolicyContext(
                    policy_version="v1",
                    decision_digest="2" * 64,
                    effective_scope="reviewed",
                    tenant_ref=tenant_ref,
                ),
                telemetry=TelemetryContext(
                    operation="queue.runtime",
                    correlation_id="correlation",
                    causation_id="cause",
                    trace_id="trace",
                    span_id="span",
                    trace_flags=1,
                    tenant_ref=tenant_ref,
                ),
            ),
            payload={},
        )
    )


def _wait_until(predicate: Any, *, timeout: float = 1.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        Event().wait(0.005)
    return bool(predicate())


@contextmanager
def _running(runtime: WorkerRuntime, *, release: Event | None = None) -> Iterator[None]:
    runtime.start()
    try:
        yield
    finally:
        if release is not None:
            release.set()
        with suppress(BaseException):
            runtime.close(timeout=timedelta(seconds=1))


def test_container_job_handler_uses_fresh_job_scope_and_projects_safe_context() -> None:
    observations: list[tuple[int, int, int, object]] = []
    closed: list[int] = []

    class Dependency:
        pass

    class Handler:
        def __init__(self, dependency: Dependency) -> None:
            self._dependency = dependency

        def __call__(self, job: QueueJob) -> None:
            del job
            context = current_context()
            observations.append((id(self), id(self._dependency), get_ident(), context))
            assert context.correlation_id == "correlation"
            assert context.causation_id == "cause"
            assert context.deadline == _NOW + timedelta(minutes=1)
            assert context.principal is None
            assert context.tenant_id is None
            assert context.trace is None
            assert context.locale is None
            assert context.requested_scope is None
            assert context.effective_scope is None

        def close(self) -> None:
            with pytest.raises(PlatformError) as unavailable:
                current_context()
            assert unavailable.value.reason is PlatformReason.CONTEXT_UNAVAILABLE
            closed.append(id(self))

    root = Container()
    root.bind(Dependency, lambda _: Dependency(), scope=Scope.INVOCATION)
    root.bind(
        Handler,
        lambda current: Handler(current.resolve(Dependency)),
        scope=Scope.INVOCATION,
    )
    application = root.application_scope()
    scope_kinds: list[InvocationKind] = []
    original_invocation_scope = application.invocation_scope

    def invocation_scope(**kwargs: Any) -> Container:
        scope_kinds.append(kwargs["kind"])
        return original_invocation_scope(**kwargs)

    application.invocation_scope = invocation_scope  # type: ignore[method-assign]
    adapter = ContainerJobHandlerFactory(application, Handler)
    calling_thread = get_ident()

    adapter(_job("runtime-job-1"))
    adapter(_job("runtime-job-2"))

    assert scope_kinds == [InvocationKind.JOB, InvocationKind.JOB]
    assert [observation[2] for observation in observations] == [calling_thread, calling_thread]
    assert observations[0][0] != observations[1][0]
    assert observations[0][1] != observations[1][1]
    assert observations[0][3] is not observations[1][3]
    assert closed == [observations[0][0], observations[1][0]]


def test_container_job_handler_projects_exact_cooperative_cancellation_token() -> None:
    observed: list[CancellationToken] = []

    class Handler:
        def __call__(self, job: QueueJob) -> None:
            del job
            observed.append(current_context().cancellation)

    root = Container()
    root.bind(Handler, lambda _: Handler(), scope=Scope.INVOCATION)
    adapter = ContainerJobHandlerFactory(root.application_scope(), Handler)
    token = CancellationToken()

    adapter.invoke(_job(), cancellation=token)

    assert observed == [token]
    assert observed[0] is token


def test_container_job_handler_allows_broad_lifetime_stateless_dispatcher() -> None:
    handled: list[str] = []

    class Dispatcher:
        def __call__(self, job: QueueJob) -> None:
            handled.append(job.envelope.metadata.message_id)

    dispatcher = Dispatcher()
    root = Container()
    root.bind(Dispatcher, lambda _: dispatcher, scope=Scope.APPLICATION, owned=False)
    adapter = ContainerJobHandlerFactory(root.application_scope(), Dispatcher)

    adapter(_job("runtime-job-1"))
    adapter(_job("runtime-job-2"))

    assert handled == ["runtime-job-1", "runtime-job-2"]


@pytest.mark.parametrize("result", [1, "unexpected"])
def test_container_job_handler_rejects_non_none_sync_result(result: object) -> None:
    class Handler:
        def __call__(self, job: QueueJob) -> object:
            del job
            return result

    root = Container()
    root.bind(Handler, lambda _: Handler(), scope=Scope.INVOCATION)

    with pytest.raises(TypeError, match="return None"):
        ContainerJobHandlerFactory(root.application_scope(), Handler)(_job())


def test_container_job_handler_rejects_async_handler_before_calling_it() -> None:
    called = False

    class AsyncHandler:
        async def __call__(self, job: QueueJob) -> None:
            del job
            nonlocal called
            called = True

    root = Container()
    root.bind(AsyncHandler, lambda _: AsyncHandler(), scope=Scope.INVOCATION)

    with pytest.raises(TypeError, match="asynchronous"):
        ContainerJobHandlerFactory(root.application_scope(), AsyncHandler)(_job())

    assert not called


def test_container_job_handler_disposes_scope_before_returning_to_settlement() -> None:
    lifecycle: list[str] = []

    class Handler:
        def __call__(self, job: QueueJob) -> None:
            del job
            lifecycle.append("handler")

        def close(self) -> None:
            lifecycle.append("dispose")

    root = Container()
    root.bind(Handler, lambda _: Handler(), scope=Scope.INVOCATION)
    adapter = ContainerJobHandlerFactory(root.application_scope(), Handler)

    adapter(_job())
    lifecycle.extend(("idempotency", "ack"))

    assert lifecycle == ["handler", "dispose", "idempotency", "ack"]


def test_container_job_handler_groups_handler_and_disposal_base_exceptions() -> None:
    handler_failure = KeyboardInterrupt()
    disposal_failure = SystemExit(2)

    class Handler:
        def __call__(self, job: QueueJob) -> None:
            del job
            raise handler_failure

        def close(self) -> None:
            raise disposal_failure

    root = Container()
    root.bind(Handler, lambda _: Handler(), scope=Scope.INVOCATION)
    adapter = ContainerJobHandlerFactory(root.application_scope(), Handler)

    with pytest.raises(BaseExceptionGroup) as captured:
        adapter(_job())

    assert captured.value.exceptions[0] is handler_failure
    assert type(captured.value.exceptions[1]) is type(disposal_failure)


def test_container_job_handler_rejects_recursive_adapter_resolution() -> None:
    application = Container().application_scope()

    with pytest.raises(TypeError, match="recursive"):
        ContainerJobHandlerFactory(application, ContainerJobHandlerFactory)


def test_container_job_handler_rejects_adapter_resolved_as_handler() -> None:
    class Handler:
        pass

    root = Container()
    adapters: list[ContainerJobHandlerFactory] = []
    root.bind(Handler, lambda _: adapters[0], scope=Scope.APPLICATION, owned=False)
    application = root.application_scope()
    adapter = ContainerJobHandlerFactory(application, Handler)
    adapters.append(adapter)

    with pytest.raises(TypeError, match="recursive"):
        adapter(_job())


class _Kernel:
    max_concurrency = 2

    def __init__(self, *, expected_workers: int = 1) -> None:
        self.expected_workers = expected_workers
        self.all_started = Event()
        self.started = 0
        self.drains = 0
        self.cancellations = 0
        self.closes = 0
        self.failures: list[BaseException] = []
        self.fail_close = False
        self._lock = Lock()

    def run_loop(
        self,
        handler: object,
        *,
        lane: str | None,
        poll_interval: timedelta,
        stop_event: Event,
    ) -> None:
        del handler, lane, poll_interval
        with self._lock:
            self.started += 1
            if self.started == self.expected_workers:
                self.all_started.set()
            failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        stop_event.wait()

    def start_draining(self) -> None:
        with self._lock:
            self.drains += 1

    def cancel_active(self) -> None:
        with self._lock:
            self.cancellations += 1

    def close(self, *, timeout: timedelta | None = None) -> None:
        del timeout
        with self._lock:
            self.closes += 1
        if self.fail_close:
            self.fail_close = False
            raise RuntimeError("close failed")


class _StubbornKernel(_Kernel):
    def __init__(self) -> None:
        super().__init__()
        self.release = Event()

    def run_loop(self, handler: object, **kwargs: Any) -> None:
        super().run_loop(handler, **kwargs)
        self.release.wait()


class _ReservingDriver:
    def __init__(self) -> None:
        self.jobs: list[QueueJob] = []
        self.acked: list[str] = []
        self.closes = 0
        self._lock = Lock()

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt:
        with self._lock:
            self.jobs.append(job)
        return QueueDispatchReceipt(
            message_id=job.envelope.metadata.message_id,
            lane=job.lane,
            disposition=QueueDispatchDisposition.ENQUEUED,
            dispatched_at=_NOW,
        )

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        with self._lock:
            for index, job in enumerate(self.jobs):
                if lane is None or job.lane == lane:
                    reserved = self.jobs.pop(index)
                    return QueueReservation(job=reserved, attempt=1, reserved_at=_NOW)
        return None

    def ack(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            self.acked.append(reservation.job.envelope.metadata.message_id)

    def reject(self, reservation: QueueReservation, /) -> None:
        del reservation

    def release(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            self.jobs.insert(0, reservation.job)

    def retry(
        self,
        reservation: QueueReservation,
        /,
        *,
        available_at: datetime | None = None,
    ) -> None:
        del available_at
        self.release(reservation)

    def close(self) -> None:
        with self._lock:
            self.closes += 1

    @property
    def queued_message_ids(self) -> list[str]:
        with self._lock:
            return [job.envelope.metadata.message_id for job in self.jobs]


def test_runtime_starts_bounded_non_daemon_workers_behind_gate() -> None:
    kernel = _Kernel(expected_workers=2)
    created: list[Thread] = []

    def thread_factory(**kwargs: Any) -> Thread:
        thread = Thread(**kwargs)
        created.append(thread)
        return thread

    runtime = WorkerRuntime(kernel, lambda job: None, worker_count=2, thread_factory=thread_factory)
    with _running(runtime):
        assert kernel.all_started.wait(timeout=1)
        assert len(created) == 2
        assert all(not thread.daemon for thread in created)
        assert runtime.state is WorkerRuntimeState.RUNNING


def test_runtime_rejects_worker_count_above_kernel_capacity() -> None:
    with pytest.raises(ValueError, match="max_concurrency"):
        WorkerRuntime(_Kernel(), lambda job: None, worker_count=3)


def test_runtime_never_runs_more_handlers_than_kernel_capacity() -> None:
    release = Event()
    both_active = Event()
    lock = Lock()
    active = 0
    peak = 0

    def handler(job: QueueJob) -> None:
        del job
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                both_active.set()
        assert release.wait(timeout=1)
        with lock:
            active -= 1

    class ConcurrentKernel(_Kernel):
        def run_loop(self, handler: object, **kwargs: Any) -> None:
            assert callable(handler)
            handler(_job(f"runtime-job-{get_ident()}"))
            kwargs["stop_event"].wait()

    kernel = ConcurrentKernel(expected_workers=2)
    runtime = WorkerRuntime(kernel, handler, worker_count=2)
    with _running(runtime, release=release):
        assert both_active.wait(timeout=1)
        assert peak == kernel.max_concurrency


def test_runtime_partial_start_rolls_back_without_entering_kernel() -> None:
    kernel = _Kernel(expected_workers=2)
    created: list[Thread] = []
    calls = 0

    class BrokenThread(Thread):
        def start(self) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("thread start failed")
            super().start()

    def thread_factory(**kwargs: Any) -> Thread:
        thread = BrokenThread(**kwargs)
        created.append(thread)
        return thread

    runtime = WorkerRuntime(
        kernel,
        lambda job: None,
        worker_count=2,
        thread_factory=thread_factory,
    )

    with pytest.raises(RuntimeError, match="thread start failed"):
        runtime.start()

    assert kernel.started == 0
    assert all(not thread.is_alive() for thread in created)
    assert runtime.state is WorkerRuntimeState.FAILED
    assert isinstance(runtime.failure, RuntimeError)
    assert kernel.closes == 1

    with pytest.raises(RuntimeError, match="thread start failed"):
        runtime.close(timeout=timedelta(seconds=1))
    assert kernel.closes == 1


def test_runtime_start_rollback_joins_thread_whose_start_starts_then_raises() -> None:
    kernel = _Kernel(expected_workers=2)
    joins: list[str] = []
    calls = 0

    class StartsThenRaisesThread(Thread):
        def start(self) -> None:
            nonlocal calls
            calls += 1
            super().start()
            if calls == 2:
                raise RuntimeError("thread start failed after start")

        def join(self, timeout: float | None = None) -> None:
            joins.append(self.name)
            super().join(timeout=timeout)

    runtime = WorkerRuntime(
        kernel,
        lambda job: None,
        worker_count=2,
        thread_factory=StartsThenRaisesThread,
    )

    with pytest.raises(RuntimeError, match="thread start failed after start"):
        runtime.start()

    assert joins == ["af-queue-worker-1", "af-queue-worker-2"]
    assert runtime.wait(timeout=timedelta(seconds=1))
    assert kernel.started == 0
    assert kernel.closes == 1


def test_runtime_start_rollback_does_not_close_kernel_after_join_failure() -> None:
    startup_failure = RuntimeError("thread start failed")
    join_failure = SystemExit(2)
    calls = 0
    join_failed = False

    class JoinFailingThread(Thread):
        def start(self) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise startup_failure
            super().start()

        def join(self, timeout: float | None = None) -> None:
            nonlocal join_failed
            super().join(timeout=timeout)
            if not join_failed:
                join_failed = True
                raise join_failure

    kernel = _Kernel(expected_workers=2)
    runtime = WorkerRuntime(
        kernel,
        lambda job: None,
        worker_count=2,
        thread_factory=JoinFailingThread,
    )

    with pytest.raises(BaseExceptionGroup) as captured:
        runtime.start()

    assert captured.value.exceptions == (startup_failure, join_failure)
    assert kernel.closes == 0
    with pytest.raises(BaseExceptionGroup) as sticky:
        runtime.close(timeout=timedelta(seconds=1))
    assert sticky.value is captured.value
    assert kernel.closes == 1


def test_runtime_start_rollback_does_not_close_kernel_while_started_thread_is_alive() -> None:
    startup_failure = RuntimeError("thread start failed")
    release = Event()
    calls = 0
    joins = 0

    class StillAliveThread(Thread):
        def start(self) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise startup_failure
            super().start()

        def run(self) -> None:
            super().run()
            release.wait()

        def join(self, timeout: float | None = None) -> None:
            nonlocal joins
            joins += 1
            if joins > 1:
                super().join(timeout=timeout)

    kernel = _Kernel(expected_workers=2)
    runtime = WorkerRuntime(
        kernel,
        lambda job: None,
        worker_count=2,
        thread_factory=StillAliveThread,
    )

    try:
        with pytest.raises(BaseExceptionGroup) as captured:
            runtime.start()

        assert captured.value.exceptions[0] is startup_failure
        liveness_failure = captured.value.exceptions[1]
        assert isinstance(liveness_failure, QueueOperationError)
        assert liveness_failure.stage == "queue.runtime.start.rollback"
        assert kernel.closes == 0
        release.set()
        with pytest.raises(BaseExceptionGroup) as sticky:
            runtime.close(timeout=timedelta(seconds=1))
        assert sticky.value is captured.value
        assert kernel.closes == 1
    finally:
        release.set()
        with suppress(BaseException):
            runtime.close(timeout=timedelta(seconds=1))


def test_runtime_start_preserves_failures_and_retries_kernel_cleanup() -> None:
    cleanup_failure = SystemExit(2)

    class CleanupFailingKernel(_Kernel):
        def close(self, *, timeout: timedelta | None = None) -> None:
            del timeout
            self.closes += 1
            if self.closes == 1:
                raise cleanup_failure

    class BrokenThread(Thread):
        def start(self) -> None:
            raise KeyboardInterrupt()

    kernel = CleanupFailingKernel()
    runtime = WorkerRuntime(kernel, lambda job: None, thread_factory=BrokenThread)

    with pytest.raises(BaseExceptionGroup) as captured:
        runtime.start()

    assert isinstance(captured.value.exceptions[0], KeyboardInterrupt)
    assert captured.value.exceptions[1] is cleanup_failure
    assert runtime.failure is captured.value
    assert runtime.state is WorkerRuntimeState.FAILED
    assert kernel.closes == 1

    with pytest.raises(BaseExceptionGroup) as sticky:
        runtime.close(timeout=timedelta(seconds=1))
    assert sticky.value is captured.value
    assert kernel.closes == 2


def test_runtime_first_fatal_infrastructure_failure_is_sticky_and_stops_siblings() -> None:
    first = RuntimeError("infrastructure failed")
    kernel = _Kernel(expected_workers=2)
    kernel.failures.append(first)
    runtime = WorkerRuntime(kernel, lambda job: None, worker_count=2)

    with _running(runtime):
        assert runtime.wait(timeout=timedelta(seconds=1))
        assert runtime.failure is first
        assert runtime.state is WorkerRuntimeState.FAILED
        assert kernel.drains == 1
        with pytest.raises(RuntimeError, match="infrastructure failed") as captured:
            runtime.close(timeout=timedelta(seconds=1))
        assert captured.value is first


def test_runtime_preserves_multiple_worker_failures() -> None:
    failures = (RuntimeError("worker one"), SystemExit(2))

    class SimultaneousFailureKernel(_Kernel):
        def run_loop(self, handler: object, **kwargs: Any) -> None:
            del handler, kwargs
            with self._lock:
                index = self.started
                self.started += 1
                if self.started == 2:
                    self.all_started.set()
            assert self.all_started.wait(timeout=1)
            raise failures[index]

    kernel = SimultaneousFailureKernel(expected_workers=2)
    runtime = WorkerRuntime(kernel, lambda job: None, worker_count=2)
    runtime.start()
    assert runtime.wait(timeout=timedelta(seconds=1))

    observed = runtime.failure
    assert isinstance(observed, BaseExceptionGroup)
    assert len(observed.exceptions) == 2
    assert {id(error) for error in observed.exceptions} == {id(error) for error in failures}

    with pytest.raises(BaseExceptionGroup) as sticky:
        runtime.close(timeout=timedelta(seconds=1))
    assert sticky.value is observed


def test_runtime_retries_failed_active_cancellation_request() -> None:
    cancellation_failure = RuntimeError("cancellation request failed")

    class CancellationFailingKernel(_Kernel):
        def cancel_active(self) -> None:
            self.cancellations += 1
            if self.cancellations == 1:
                raise cancellation_failure

    kernel = CancellationFailingKernel()
    runtime = WorkerRuntime(kernel, lambda job: None)
    runtime.start()
    assert kernel.all_started.wait(timeout=1)

    with pytest.raises(RuntimeError, match="cancellation request failed") as failed:
        runtime.close(timeout=timedelta(seconds=1))
    assert failed.value is cancellation_failure
    assert kernel.closes == 0

    runtime.close(timeout=timedelta(seconds=1))
    assert kernel.cancellations == 2
    assert kernel.closes == 1
    assert runtime.state is WorkerRuntimeState.STOPPED


@pytest.mark.parametrize("blocked_operation", ["drain", "cancel"])
def test_runtime_timeout_bounds_concurrent_shutdown_coordination(
    blocked_operation: str,
) -> None:
    entered = Event()
    release = Event()
    first_failures: list[BaseException] = []

    class BlockingCoordinationKernel(_Kernel):
        def start_draining(self) -> None:
            if blocked_operation == "drain":
                entered.set()
                assert release.wait(timeout=2)
            super().start_draining()

        def cancel_active(self) -> None:
            if blocked_operation == "cancel":
                entered.set()
                assert release.wait(timeout=2)
            super().cancel_active()

    kernel = BlockingCoordinationKernel()
    runtime = WorkerRuntime(kernel, lambda job: None)
    first_operation = runtime.drain if blocked_operation == "drain" else runtime.close

    def first_shutdown() -> None:
        try:
            first_operation(timeout=timedelta(seconds=1))
        except BaseException as error:
            first_failures.append(error)

    first = Thread(target=first_shutdown)
    try:
        runtime.start()
        assert kernel.all_started.wait(timeout=1)
        first.start()
        assert entered.wait(timeout=1)

        started = monotonic()
        with pytest.raises(QueueOperationError) as timeout:
            runtime.close(timeout=timedelta(milliseconds=20))
        elapsed = monotonic() - started

        assert timeout.value.reason is PlatformReason.SHUTDOWN_FAILED
        assert timeout.value.stage == "queue.runtime.shutdown"
        assert elapsed < 0.5
        release.set()
        first.join(timeout=1)
        assert not first.is_alive()
        assert first_failures == []
        runtime.close(timeout=timedelta(seconds=1))
    finally:
        release.set()
        if first.ident is not None:
            first.join(timeout=1)
        with suppress(BaseException):
            runtime.close(timeout=timedelta(seconds=1))


def test_runtime_does_not_start_cancellation_after_drain_crosses_deadline() -> None:
    entered_drain = Event()
    release_drain = Event()

    class SlowDrainKernel(_Kernel):
        def start_draining(self) -> None:
            entered_drain.set()
            assert release_drain.wait(timeout=1)
            super().start_draining()

    kernel = SlowDrainKernel()
    runtime = WorkerRuntime(kernel, lambda job: None)

    def delayed_release() -> None:
        assert entered_drain.wait(timeout=1)
        Event().wait(0.05)
        release_drain.set()

    releaser = Thread(target=delayed_release)
    try:
        runtime.start()
        releaser.start()
        with pytest.raises(QueueOperationError) as timeout:
            runtime.close(timeout=timedelta(milliseconds=20))

        assert timeout.value.stage == "queue.runtime.shutdown"
        assert kernel.drains == 1
        assert kernel.cancellations == 0

        runtime.close(timeout=timedelta(seconds=1))
        assert kernel.cancellations == 1
        assert kernel.closes == 1
    finally:
        release_drain.set()
        if releaser.ident is not None:
            releaser.join(timeout=1)
        with suppress(BaseException):
            runtime.close(timeout=timedelta(seconds=1))


def test_runtime_groups_sticky_worker_and_simultaneous_kernel_close_failures() -> None:
    worker_failure = RuntimeError("infrastructure failed")
    kernel = _Kernel()
    kernel.failures.append(worker_failure)
    kernel.fail_close = True
    runtime = WorkerRuntime(kernel, lambda job: None)

    with _running(runtime):
        assert runtime.wait(timeout=timedelta(seconds=1))
        with pytest.raises(BaseExceptionGroup) as captured:
            runtime.close(timeout=timedelta(seconds=1))
        assert captured.value.exceptions[0] is worker_failure
        assert isinstance(captured.value.exceptions[1], RuntimeError)
        assert str(captured.value.exceptions[1]) == "close failed"
        with pytest.raises(RuntimeError, match="infrastructure failed") as sticky:
            runtime.close(timeout=timedelta(seconds=1))
        assert sticky.value is worker_failure
        assert kernel.closes == 2


def test_runtime_remains_healthy_when_kernel_continues_after_settled_handler_failure() -> None:
    handled: list[str] = []

    class SettlingKernel(_Kernel):
        def run_loop(self, handler: object, **kwargs: Any) -> None:
            assert callable(handler)
            for message_id in ("failed", "continued"):
                try:
                    handler(_job(message_id))
                except ValueError:
                    continue
            kwargs["stop_event"].wait()

    def handler(job: QueueJob) -> None:
        message_id = job.envelope.metadata.message_id
        if message_id == "failed":
            raise ValueError("settled handler failure")
        handled.append(message_id)

    runtime = WorkerRuntime(SettlingKernel(), handler)
    with _running(runtime):
        assert _wait_until(lambda: handled == ["continued"])
        assert runtime.failure is None
        assert runtime.state is WorkerRuntimeState.RUNNING


@pytest.mark.parametrize(
    ("operation", "stage"),
    [("drain", "queue.runtime.self-drain"), ("close", "queue.runtime.self-close")],
)
def test_runtime_owned_worker_rejects_lifecycle_calls(operation: str, stage: str) -> None:
    failures: list[BaseException] = []
    attempted = Event()
    runtime: WorkerRuntime

    def handler(job: QueueJob) -> None:
        del job
        try:
            getattr(runtime, operation)(timeout=timedelta(seconds=1))
        except BaseException as error:
            failures.append(error)
        finally:
            attempted.set()

    class CallingKernel(_Kernel):
        def run_loop(self, handler: object, **kwargs: Any) -> None:
            assert callable(handler)
            handler(_job())
            kwargs["stop_event"].wait()

    runtime = WorkerRuntime(CallingKernel(), handler)
    with _running(runtime):
        assert attempted.wait(timeout=1)
        assert len(failures) == 1
        assert isinstance(failures[0], QueueOperationError)
        assert failures[0].reason is PlatformReason.SHUTDOWN_FAILED
        assert failures[0].stage == stage


def test_runtime_drain_timeout_is_retryable() -> None:
    kernel = _StubbornKernel()
    runtime = WorkerRuntime(kernel, lambda job: None)
    with _running(runtime, release=kernel.release):
        assert kernel.all_started.wait(timeout=1)
        with pytest.raises(QueueOperationError) as timeout:
            runtime.drain(timeout=timedelta(milliseconds=20))
        assert timeout.value.stage == "queue.runtime.shutdown"
        kernel.release.set()
        runtime.drain(timeout=timedelta(seconds=1))
        runtime.close(timeout=timedelta(seconds=1))
        assert kernel.drains == 1
        assert kernel.cancellations == 1
        assert kernel.closes == 1


def test_runtime_close_timeout_and_kernel_close_failure_are_retryable() -> None:
    kernel = _StubbornKernel()
    kernel.fail_close = True
    runtime = WorkerRuntime(kernel, lambda job: None)
    with _running(runtime, release=kernel.release):
        assert kernel.all_started.wait(timeout=1)
        with pytest.raises(QueueOperationError) as timeout:
            runtime.close(timeout=timedelta(milliseconds=20))
        assert timeout.value.stage == "queue.runtime.shutdown"
        assert kernel.closes == 0
        kernel.release.set()
        with pytest.raises(RuntimeError, match="close failed"):
            runtime.close(timeout=timedelta(seconds=1))
        runtime.close(timeout=timedelta(seconds=1))
        assert kernel.closes == 2


def test_concurrent_external_close_closes_kernel_exactly_once() -> None:
    entered_close = Event()
    release_close = Event()

    class BlockingCloseKernel(_Kernel):
        def close(self, *, timeout: timedelta | None = None) -> None:
            del timeout
            with self._lock:
                self.closes += 1
            entered_close.set()
            assert release_close.wait(timeout=1)

    kernel = BlockingCloseKernel()
    runtime = WorkerRuntime(kernel, lambda job: None)
    start_callers = Event()
    outcomes: list[BaseException | None] = []

    def close() -> None:
        start_callers.wait()
        try:
            runtime.close(timeout=timedelta(seconds=1))
        except BaseException as error:
            outcomes.append(error)
        else:
            outcomes.append(None)

    callers = [Thread(target=close) for _ in range(2)]
    runtime.start()
    try:
        for caller in callers:
            caller.start()
        start_callers.set()
        assert entered_close.wait(timeout=1)
        release_close.set()
        for caller in callers:
            caller.join(timeout=1)
            assert not caller.is_alive()

        assert outcomes == [None, None]
        assert kernel.drains == 1
        assert kernel.closes == 1
        assert runtime.state is WorkerRuntimeState.STOPPED
    finally:
        start_callers.set()
        release_close.set()
        for caller in callers:
            if caller.ident is not None:
                caller.join(timeout=1)
        runtime.close(timeout=timedelta(seconds=1))


def test_runtime_composes_worker_kernel_driver_and_container_scope_deterministically() -> None:
    entered = Event()
    release = Event()
    drain_finished = Event()
    drain_failures: list[BaseException] = []
    handled: list[str] = []
    disposed: list[str] = []

    class Handler:
        def __call__(self, job: QueueJob) -> None:
            message_id = job.envelope.metadata.message_id
            handled.append(message_id)
            entered.set()
            assert release.wait(timeout=1)

        def close(self) -> None:
            disposed.append("handler")

    root = Container()
    root.bind(Handler, lambda _: Handler(), scope=Scope.INVOCATION)
    adapter = ContainerJobHandlerFactory(root.application_scope(), Handler)
    driver = _ReservingDriver()
    driver.dispatch(_job("runtime-job-1"))
    driver.dispatch(_job("runtime-job-2"))
    kernel = WorkerKernel(driver, max_concurrency=1, clock=lambda: _NOW)
    runtime = WorkerRuntime(kernel, adapter)
    drain_thread: Thread | None = None

    def drain() -> None:
        try:
            runtime.drain(timeout=timedelta(seconds=1))
        except BaseException as error:
            drain_failures.append(error)
        finally:
            drain_finished.set()

    try:
        runtime.start()
        assert entered.wait(timeout=1)
        drain_thread = Thread(target=drain)
        drain_thread.start()
        assert _wait_until(lambda: runtime.state is WorkerRuntimeState.DRAINING)
        assert driver.queued_message_ids == ["runtime-job-2"]

        release.set()
        assert drain_finished.wait(timeout=1)
        drain_thread.join(timeout=1)
        assert not drain_thread.is_alive()
        assert drain_failures == []
        assert handled == ["runtime-job-1"]
        assert driver.acked == ["runtime-job-1"]
        assert driver.queued_message_ids == ["runtime-job-2"]
        assert disposed == ["handler"]

        runtime.close(timeout=timedelta(seconds=1))
        runtime.close(timeout=timedelta(seconds=1))
        assert driver.closes == 1
        assert runtime.state is WorkerRuntimeState.STOPPED
    finally:
        release.set()
        if drain_thread is not None:
            drain_thread.join(timeout=1)
        runtime.close(timeout=timedelta(seconds=1))


def test_runtime_close_cooperatively_cancels_and_releases_container_job() -> None:
    entered = Event()
    cancellation_observed = Event()
    disposed: list[str] = []

    class Handler:
        def __call__(self, job: QueueJob) -> None:
            del job
            token = current_context().cancellation
            entered.set()
            while not token.cancelled:
                Event().wait(0.005)
            cancellation_observed.set()

        def close(self) -> None:
            disposed.append("handler")

    root = Container()
    root.bind(Handler, lambda _: Handler(), scope=Scope.INVOCATION)
    driver = _ReservingDriver()
    driver.dispatch(_job("runtime-job-active"))
    driver.dispatch(_job("runtime-job-queued"))
    kernel = WorkerKernel(driver, max_concurrency=1, clock=lambda: _NOW)
    runtime = WorkerRuntime(
        kernel,
        ContainerJobHandlerFactory(root.application_scope(), Handler),
    )

    runtime.start()
    assert entered.wait(timeout=1)
    runtime.close(timeout=timedelta(seconds=1))

    assert cancellation_observed.is_set()
    assert driver.acked == []
    assert driver.queued_message_ids == ["runtime-job-active", "runtime-job-queued"]
    assert disposed == ["handler"]
    assert kernel.stats.cancellations == 1
    assert driver.closes == 1
    assert runtime.state is WorkerRuntimeState.STOPPED


def test_runtime_close_timeout_never_releases_handler_that_ignores_cancellation() -> None:
    entered = Event()
    release_handler = Event()
    observed_tokens: list[CancellationToken] = []

    class Handler:
        def __call__(self, job: QueueJob) -> None:
            del job
            observed_tokens.append(current_context().cancellation)
            entered.set()
            assert release_handler.wait(timeout=2)

    root = Container()
    root.bind(Handler, lambda _: Handler(), scope=Scope.INVOCATION)
    driver = _ReservingDriver()
    driver.dispatch(_job("runtime-job-ignored-cancellation"))
    kernel = WorkerKernel(driver, max_concurrency=1, clock=lambda: _NOW)
    runtime = WorkerRuntime(
        kernel,
        ContainerJobHandlerFactory(root.application_scope(), Handler),
    )

    try:
        runtime.start()
        assert entered.wait(timeout=1)
        with pytest.raises(QueueOperationError) as timeout:
            runtime.close(timeout=timedelta(milliseconds=20))

        assert timeout.value.reason is PlatformReason.SHUTDOWN_FAILED
        assert timeout.value.stage == "queue.runtime.shutdown"
        assert observed_tokens[0].cancelled
        assert driver.queued_message_ids == []
        assert driver.acked == []
        assert driver.closes == 0
        assert runtime.state is WorkerRuntimeState.DRAINING

        release_handler.set()
        runtime.close(timeout=timedelta(seconds=1))

        assert driver.queued_message_ids == ["runtime-job-ignored-cancellation"]
        assert driver.acked == []
        assert driver.closes == 1
        assert runtime.state is WorkerRuntimeState.STOPPED
    finally:
        release_handler.set()
        runtime.close(timeout=timedelta(seconds=1))


def test_queue_public_api_exports_owned_runtime() -> None:
    from af.queue import (
        ContainerJobHandlerFactory,
        WorkerRuntime,
        WorkerRuntimeKernel,
        WorkerRuntimeState,
    )

    assert ContainerJobHandlerFactory is not None
    assert WorkerRuntime is not None
    assert WorkerRuntimeKernel is not None
    assert WorkerRuntimeState is not None
