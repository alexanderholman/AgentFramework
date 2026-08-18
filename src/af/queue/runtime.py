"""Owned synchronous queue worker runtime and container handler adapter."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from threading import Condition, Event, RLock, Thread, get_ident
from time import monotonic
from typing import Protocol, TypeVar, cast, runtime_checkable

from af.framework.container import Container, InvocationKind
from af.framework.context import CancellationToken, ExecutionContext
from af.framework.errors import PlatformReason, ScopeDisposalError
from af.queue.contracts import QueueJob, QueueOperationError
from af.queue.worker import JobHandler

__all__ = (
    "ContainerJobHandlerFactory",
    "OwnedManagerConnectionRecycler",
    "PurgeableConnectionManager",
    "WorkerRuntime",
    "WorkerRuntimeKernel",
    "WorkerRuntimeState",
    "WorkerStopSignal",
    "WorkerSupervisor",
    "WorkerSupervisorMaintenance",
    "WorkerSupervisorState",
)

_HandlerT = TypeVar("_HandlerT")


class WorkerRuntimeState(StrEnum):
    """Lifecycle states for a runtime that owns worker threads."""

    IDLE = "idle"
    RUNNING = "running"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


class WorkerSupervisorState(StrEnum):
    """Lifecycle states for a blocking worker-generation supervisor."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class WorkerRuntimeKernel(Protocol):
    """Kernel lifecycle required by an owned synchronous worker runtime."""

    @property
    def max_concurrency(self) -> int: ...

    def run_loop(
        self,
        consumer: JobHandler,
        /,
        *,
        lane: str | None,
        poll_interval: timedelta,
        stop_event: Event,
    ) -> None: ...

    def start_draining(self) -> None: ...

    def close(self, *, timeout: timedelta | None = None) -> None: ...


@runtime_checkable
class WorkerStopSignal(Protocol):
    """Cross-thread or cross-process stop signal accepted by a supervisor."""

    def is_set(self) -> bool: ...

    def set(self) -> None: ...

    def wait(self, timeout: float | None = None) -> bool: ...


@runtime_checkable
class WorkerSupervisorMaintenance(Protocol):
    """Synchronous maintenance owned by the worker supervisor cadence."""

    def run(self) -> None: ...


class PurgeableConnectionManager(Protocol):
    """Synchronous manager whose cached connections can be recycled."""

    def purge(self) -> None: ...


class OwnedManagerConnectionRecycler:
    """Recycle connections held by managers owned exclusively by one worker kernel."""

    def __init__(self, *managers: PurgeableConnectionManager) -> None:
        if not managers:
            raise ValueError("worker connection recycler requires at least one manager")
        if len({id(manager) for manager in managers}) != len(managers):
            raise ValueError("worker connection recycler managers must be unique")
        for manager in managers:
            purge = getattr(manager, "purge", None)
            if not callable(purge):
                raise TypeError("worker connection recycler manager requires callable purge")
            if inspect.iscoroutinefunction(purge) or inspect.isasyncgenfunction(purge):
                raise TypeError("worker connection recycler manager purge must be synchronous")
        self._managers = managers

    def recycle(self) -> None:
        failures: list[BaseException] = []
        for manager in self._managers:
            try:
                purge = cast(Callable[[], object], manager.purge)
                result = purge()
                if inspect.iscoroutine(result):
                    result.close()
                    raise TypeError("worker connection manager returned an asynchronous result")
                if inspect.isawaitable(result) or inspect.isasyncgen(result):
                    raise TypeError("worker connection manager returned an asynchronous result")
                if result is not None:
                    raise TypeError("worker connection manager purge must return None")
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            failure = failures[0]
            raise failure.with_traceback(failure.__traceback__)
        if failures:
            raise BaseExceptionGroup(
                "worker connection manager purges failed",
                tuple(failures),
            ) from None


@runtime_checkable
class _ActiveCancellationKernel(Protocol):
    def cancel_active(self) -> None: ...


def _validate_sync_handler(handler: object) -> None:
    if not callable(handler):
        raise TypeError("queue job handler must be callable")
    if inspect.iscoroutinefunction(handler) or inspect.isasyncgenfunction(handler):
        raise TypeError("queue job handler must not be asynchronous")
    call = inspect.getattr_static(handler, "__call__", None)
    if inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call):
        raise TypeError("queue job handler must not be asynchronous")


class ContainerJobHandlerFactory:
    """Resolve and dispose a fresh synchronous handler for every queue job."""

    cooperative_cancellation = True

    def __init__(self, application: Container, handler_type: type[_HandlerT]) -> None:
        if not isinstance(application, Container):
            raise TypeError("container job handler requires an application container")
        if not isinstance(handler_type, type):
            raise TypeError("container job handler token must be a type")
        if issubclass(handler_type, ContainerJobHandlerFactory):
            raise TypeError("recursive container job handler adapters are not supported")
        self._application = application
        self._handler_type = handler_type

    def __call__(self, job: QueueJob) -> None:
        self.invoke(job, cancellation=CancellationToken())

    def invoke(
        self,
        job: QueueJob,
        /,
        *,
        cancellation: CancellationToken,
    ) -> None:
        if not isinstance(job, QueueJob):
            raise TypeError("container job handler requires a QueueJob")
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("container job handler cancellation must be a CancellationToken")
        metadata = job.envelope.metadata
        context = ExecutionContext(
            correlation_id=metadata.correlation_id,
            causation_id=metadata.causation_id,
            deadline=metadata.deadline,
            cancellation=cancellation,
        )
        scope = self._application.invocation_scope(
            context=context,
            kind=InvocationKind.JOB,
        )
        failure: BaseException | None = None
        try:
            scope.__enter__()
            handler = scope.resolve(self._handler_type)
            if isinstance(handler, ContainerJobHandlerFactory):
                raise TypeError("recursive container job handler adapters are not supported")
            _validate_sync_handler(handler)
            result = cast(Callable[[QueueJob], object], handler)(job)
            if inspect.iscoroutine(result):
                result.close()
                raise TypeError("queue job handler returned an asynchronous result")
            if inspect.isawaitable(result) or inspect.isasyncgen(result):
                raise TypeError("queue job handler returned an asynchronous result")
            if result is not None:
                raise TypeError("queue job handler must return None")
        except BaseException as error:
            failure = error

        try:
            scope.__exit__(
                type(failure) if failure is not None else None,
                failure,
                failure.__traceback__ if failure is not None else None,
            )
        except BaseException as disposal_failure:
            disposal_failures = (
                disposal_failure.failures
                if isinstance(disposal_failure, ScopeDisposalError)
                else (disposal_failure,)
            )
            if failure is not None:
                raise BaseExceptionGroup(
                    "queue job handler and scope disposal both failed",
                    (failure, *disposal_failures),
                ) from None
            if len(disposal_failures) == 1:
                raise disposal_failures[0].with_traceback(
                    disposal_failures[0].__traceback__
                ) from None
            raise BaseExceptionGroup(
                "queue job scope disposal failed",
                disposal_failures,
            ) from None
        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)


class WorkerRuntime:
    """Own a bounded set of non-daemon threads driving one worker kernel."""

    def __init__(
        self,
        kernel: WorkerRuntimeKernel,
        handler: JobHandler,
        *,
        worker_count: int = 1,
        lane: str | None = None,
        poll_interval: timedelta = timedelta(milliseconds=100),
        thread_factory: Callable[..., Thread] = Thread,
    ) -> None:
        if type(worker_count) is not int or worker_count < 1:
            raise ValueError("worker runtime count must be positive")
        capacity = kernel.max_concurrency
        if type(capacity) is not int or capacity < 1:
            raise ValueError("worker kernel max_concurrency must be positive")
        if worker_count > capacity:
            raise ValueError("worker count exceeds kernel max_concurrency")
        if poll_interval <= timedelta(0):
            raise ValueError("worker runtime poll interval must be positive")
        _validate_sync_handler(handler)
        if not callable(thread_factory):
            raise TypeError("worker runtime thread factory must be callable")

        self._kernel = kernel
        self._handler = handler
        self._worker_count = worker_count
        self._lane = lane
        self._poll_interval = poll_interval
        self._thread_factory = thread_factory
        self._stop = Event()
        self._start_gate = Event()
        self._threads: list[Thread] = []
        self._owned_thread_ids: set[int] = set()
        self._state = WorkerRuntimeState.IDLE
        self._failure: BaseException | None = None
        self._drain_started = False
        self._drain_starting = False
        self._active_cancellation_requested = False
        self._active_cancellation_requesting = False
        self._closing = False
        self._closed = False
        self._condition = Condition(RLock())

    @property
    def state(self) -> WorkerRuntimeState:
        with self._condition:
            return self._state

    @property
    def worker_count(self) -> int:
        return self._worker_count

    @property
    def max_concurrency(self) -> int:
        return self._kernel.max_concurrency

    @property
    def lane(self) -> str | None:
        return self._lane

    @property
    def poll_interval(self) -> timedelta:
        return self._poll_interval

    @property
    def failure(self) -> BaseException | None:
        with self._condition:
            return self._failure

    @property
    def closed(self) -> bool:
        """Return whether the owned kernel completed its close lifecycle."""

        with self._condition:
            return self._closed

    def start(self) -> None:
        start_failure: BaseException | None = None
        attempted: list[Thread] = []
        with self._condition:
            if self._state is not WorkerRuntimeState.IDLE:
                raise QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="queue.runtime.start",
                )
            try:
                for index in range(self._worker_count):
                    thread = self._thread_factory(
                        target=self._run_worker,
                        name=f"af-queue-worker-{index + 1}",
                        daemon=False,
                    )
                    if not isinstance(thread, Thread):
                        raise TypeError("worker runtime thread factory must return a Thread")
                    self._threads.append(thread)
                for thread in self._threads:
                    attempted.append(thread)
                    thread.start()
            except BaseException as error:
                self._record_failure_locked(error)
                self._stop.set()
                self._start_gate.set()
                self._closing = True
                start_failure = error
            else:
                self._state = WorkerRuntimeState.RUNNING
                self._start_gate.set()
        if start_failure is not None:
            cleanup_failures: list[BaseException] = []
            threads_stopped = True
            for thread in attempted:
                if thread.ident is not None:
                    try:
                        thread.join()
                    except BaseException as error:
                        cleanup_failures.append(error)
                        threads_stopped = False
                        continue
                    if thread.is_alive():
                        cleanup_failures.append(
                            QueueOperationError(
                                reason=PlatformReason.SHUTDOWN_FAILED,
                                stage="queue.runtime.start.rollback",
                            )
                        )
                        threads_stopped = False
            kernel_closed = False
            if threads_stopped:
                try:
                    self._kernel.close()
                except BaseException as error:
                    cleanup_failures.append(error)
                else:
                    kernel_closed = True
            failure = (
                start_failure
                if not cleanup_failures
                else BaseExceptionGroup(
                    "queue worker startup and rollback failed",
                    (start_failure, *cleanup_failures),
                )
            )
            with self._condition:
                self._failure = failure
                self._closing = False
                self._closed = kernel_closed
                self._condition.notify_all()
            raise failure.with_traceback(failure.__traceback__)

    def wait(self, *, timeout: timedelta | None = None) -> bool:
        deadline = self._deadline(timeout)
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(timeout=self._remaining(deadline))
            if thread.is_alive():
                return False
        return True

    def drain(self, *, timeout: timedelta | None = None) -> None:
        deadline = self._deadline(timeout)
        self._assert_external("queue.runtime.self-drain")
        self._begin_drain(cancel_active=False, deadline=deadline)
        self._join_workers(deadline)

    def close(self, *, timeout: timedelta | None = None) -> None:
        deadline = self._deadline(timeout)
        self._assert_external("queue.runtime.self-close")
        self._begin_drain(cancel_active=True, deadline=deadline)
        self._join_workers(deadline)

        with self._condition:
            while self._closing:
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    self._raise_timeout()
                self._condition.wait(timeout=remaining)
            if self._closed:
                failure = self._failure
                if failure is not None:
                    raise failure.with_traceback(failure.__traceback__)
                return
            self._closing = True
        try:
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                self._raise_timeout()
            self._kernel.close(timeout=None if remaining is None else timedelta(seconds=remaining))
        except BaseException as close_failure:
            with self._condition:
                self._closing = False
                failure = self._failure
                self._condition.notify_all()
            if failure is not None:
                raise BaseExceptionGroup(
                    "queue worker and kernel close both failed",
                    (failure, close_failure),
                ) from None
            raise
        with self._condition:
            self._closing = False
            self._closed = True
            if self._failure is None:
                self._state = WorkerRuntimeState.STOPPED
            failure = self._failure
            self._condition.notify_all()
        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)

    def _run_worker(self) -> None:
        thread_id = get_ident()
        with self._condition:
            self._owned_thread_ids.add(thread_id)
        try:
            self._start_gate.wait()
            if self._stop.is_set():
                return
            self._kernel.run_loop(
                self._handler,
                lane=self._lane,
                poll_interval=self._poll_interval,
                stop_event=self._stop,
            )
            if not self._stop.is_set():
                raise QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="queue.runtime.worker-exit",
                )
        except BaseException as error:
            with self._condition:
                first = self._failure is None
                self._record_failure_locked(error)
            if first:
                try:
                    self._begin_drain(cancel_active=True, deadline=None)
                except BaseException as drain_failure:
                    with self._condition:
                        self._record_failure_locked(drain_failure)
        finally:
            with self._condition:
                self._owned_thread_ids.discard(thread_id)
                self._condition.notify_all()

    def _begin_drain(self, *, cancel_active: bool, deadline: float | None) -> None:
        with self._condition:
            if self._closed:
                return
            self._stop.set()
            if self._failure is None:
                self._state = WorkerRuntimeState.DRAINING
        self._apply_drain(deadline)
        if cancel_active:
            self._apply_active_cancellation(deadline)

    def _apply_drain(self, deadline: float | None) -> None:
        with self._condition:
            while self._drain_starting:
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    self._raise_timeout()
                self._condition.wait(timeout=remaining)
            if self._drain_started:
                return
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                self._raise_timeout()
            self._drain_starting = True
        try:
            self._kernel.start_draining()
        except BaseException:
            with self._condition:
                self._drain_starting = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._drain_started = True
            self._drain_starting = False
            self._condition.notify_all()

    def _apply_active_cancellation(self, deadline: float | None) -> None:
        with self._condition:
            while self._active_cancellation_requesting:
                remaining = self._remaining(deadline)
                if remaining is not None and remaining <= 0:
                    self._raise_timeout()
                self._condition.wait(timeout=remaining)
            if self._active_cancellation_requested:
                return
            remaining = self._remaining(deadline)
            if remaining is not None and remaining <= 0:
                self._raise_timeout()
            self._active_cancellation_requesting = True
        try:
            if isinstance(self._kernel, _ActiveCancellationKernel):
                self._kernel.cancel_active()
        except BaseException:
            with self._condition:
                self._active_cancellation_requesting = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._active_cancellation_requested = True
            self._active_cancellation_requesting = False
            self._condition.notify_all()

    def _join_workers(self, deadline: float | None) -> None:
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(timeout=self._remaining(deadline))
            if thread.is_alive():
                self._raise_timeout()

    def _record_failure_locked(self, error: BaseException) -> None:
        if self._failure is None:
            self._failure = error
        elif isinstance(self._failure, BaseExceptionGroup) and (
            self._failure.message == "queue worker failures"
        ):
            self._failure = BaseExceptionGroup(
                "queue worker failures",
                (*self._failure.exceptions, error),
            )
        else:
            self._failure = BaseExceptionGroup(
                "queue worker failures",
                (self._failure, error),
            )
        self._state = WorkerRuntimeState.FAILED
        self._stop.set()
        self._condition.notify_all()

    def _assert_external(self, stage: str) -> None:
        with self._condition:
            if get_ident() in self._owned_thread_ids:
                raise QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage=stage,
                )

    @staticmethod
    def _deadline(timeout: timedelta | None) -> float | None:
        if timeout is not None and timeout <= timedelta(0):
            raise ValueError("worker runtime shutdown timeout must be positive")
        return None if timeout is None else monotonic() + timeout.total_seconds()

    @staticmethod
    def _remaining(deadline: float | None) -> float | None:
        return None if deadline is None else max(0.0, deadline - monotonic())

    @staticmethod
    def _raise_timeout() -> None:
        raise QueueOperationError(
            reason=PlatformReason.SHUTDOWN_FAILED,
            stage="queue.runtime.shutdown",
        )


class WorkerSupervisor:
    """Run and replace failed worker-runtime generations within a fixed budget.

    ``run`` intentionally blocks so a deployment entrypoint can keep supervision in
    code while an external signal adapter owns the supplied stop event. Each restart
    receives a fresh runtime and therefore a fresh kernel from ``runtime_factory``.
    """

    def __init__(
        self,
        runtime_factory: Callable[[], WorkerRuntime],
        *,
        max_restarts: int = 0,
        restart_delay: timedelta = timedelta(0),
        watch_interval: timedelta = timedelta(milliseconds=100),
        shutdown_timeout: timedelta = timedelta(seconds=30),
        maintenance: WorkerSupervisorMaintenance | None = None,
        maintenance_interval: timedelta = timedelta(seconds=30),
    ) -> None:
        if not callable(runtime_factory):
            raise TypeError("worker supervisor runtime factory must be callable")
        if type(max_restarts) is not int or max_restarts < 0:
            raise ValueError("worker supervisor restart budget must be non-negative")
        if restart_delay < timedelta(0):
            raise ValueError("worker supervisor restart delay must be non-negative")
        if watch_interval <= timedelta(0):
            raise ValueError("worker supervisor watch interval must be positive")
        if shutdown_timeout <= timedelta(0):
            raise ValueError("worker supervisor shutdown timeout must be positive")
        if maintenance_interval <= timedelta(0):
            raise ValueError("worker supervisor maintenance interval must be positive")
        if maintenance is not None:
            if not isinstance(maintenance, WorkerSupervisorMaintenance):
                raise TypeError("worker supervisor maintenance requires callable run")
            run = inspect.getattr_static(maintenance, "run", None)
            if inspect.iscoroutinefunction(run) or inspect.isasyncgenfunction(run):
                raise TypeError("worker supervisor maintenance must be synchronous")
        self._runtime_factory = runtime_factory
        self._max_restarts = max_restarts
        self._restart_delay_seconds = restart_delay.total_seconds()
        self._watch_interval = watch_interval
        self._shutdown_timeout = shutdown_timeout
        self._maintenance = maintenance
        self._maintenance_interval = maintenance_interval
        self._maintenance_interval_seconds = maintenance_interval.total_seconds()
        self._maintenance_run_count = 0
        self._state = WorkerSupervisorState.IDLE
        self._failure: BaseException | None = None
        self._failures: list[BaseException] = []
        self._generation_count = 0
        self._restart_count = 0
        self._lock = RLock()

    @property
    def state(self) -> WorkerSupervisorState:
        with self._lock:
            return self._state

    @property
    def failure(self) -> BaseException | None:
        with self._lock:
            return self._failure

    @property
    def failures(self) -> tuple[BaseException, ...]:
        with self._lock:
            return tuple(self._failures)

    @property
    def generation_count(self) -> int:
        with self._lock:
            return self._generation_count

    @property
    def restart_count(self) -> int:
        with self._lock:
            return self._restart_count

    @property
    def maintenance_interval(self) -> timedelta:
        return self._maintenance_interval

    @property
    def maintenance_run_count(self) -> int:
        with self._lock:
            return self._maintenance_run_count

    def run(self, *, stop_event: WorkerStopSignal) -> None:
        if not isinstance(stop_event, WorkerStopSignal):
            raise TypeError("worker supervisor stop_event must be a WorkerStopSignal")
        with self._lock:
            if self._state is not WorkerSupervisorState.IDLE:
                raise QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="queue.supervisor.start",
                )
            self._state = WorkerSupervisorState.RUNNING

        if stop_event.is_set():
            self._mark_stopped()
            return

        while True:
            runtime: WorkerRuntime | None = None
            next_maintenance: float | None = None
            try:
                if self._maintenance is not None:
                    self._run_maintenance()
                    next_maintenance = monotonic() + self._maintenance_interval_seconds
                candidate = self._runtime_factory()
                if not isinstance(candidate, WorkerRuntime):
                    raise TypeError("worker supervisor runtime factory must return a WorkerRuntime")
                runtime = candidate
                with self._lock:
                    self._generation_count += 1
                runtime.start()
                while not stop_event.is_set():
                    if (
                        next_maintenance is not None
                        and monotonic() >= next_maintenance
                        and not stop_event.is_set()
                    ):
                        self._run_maintenance()
                        next_maintenance = monotonic() + self._maintenance_interval_seconds
                    wait_interval = self._watch_interval
                    if next_maintenance is not None:
                        until_maintenance = next_maintenance - monotonic()
                        if until_maintenance > 0:
                            wait_interval = min(
                                wait_interval,
                                timedelta(seconds=until_maintenance),
                            )
                    if runtime.wait(timeout=wait_interval):
                        break

                runtime_failure = runtime.failure
                if runtime_failure is not None:
                    raise runtime_failure.with_traceback(runtime_failure.__traceback__)
                if not stop_event.is_set():
                    raise QueueOperationError(
                        reason=PlatformReason.SHUTDOWN_FAILED,
                        stage="queue.supervisor.worker-loss",
                    )
                runtime.close(timeout=self._shutdown_timeout)
            except BaseException as error:
                failure, retired = self._retire_failed_runtime(
                    runtime,
                    error,
                    timeout=self._shutdown_timeout,
                )
                with self._lock:
                    self._failures.append(failure)
                    exhausted = len(self._failures) > self._max_restarts
                if not retired or exhausted:
                    self._fail_closed()
                if stop_event.wait(self._restart_delay_seconds):
                    self._mark_stopped()
                    return
                with self._lock:
                    self._restart_count += 1
                continue

            self._mark_stopped()
            return

    def _run_maintenance(self) -> None:
        maintenance = self._maintenance
        assert maintenance is not None
        run = cast(Callable[[], object], maintenance.run)
        result = run()
        if inspect.iscoroutine(result):
            result.close()
            raise TypeError("worker supervisor maintenance returned an asynchronous result")
        if inspect.isawaitable(result) or inspect.isasyncgen(result):
            raise TypeError("worker supervisor maintenance returned an asynchronous result")
        if result is not None:
            raise TypeError("worker supervisor maintenance must return None")
        with self._lock:
            self._maintenance_run_count += 1

    @staticmethod
    def _retire_failed_runtime(
        runtime: WorkerRuntime | None,
        failure: BaseException,
        *,
        timeout: timedelta,
    ) -> tuple[BaseException, bool]:
        if runtime is None:
            return failure, True
        try:
            runtime.close(timeout=timeout)
        except BaseException as cleanup_failure:
            if cleanup_failure is failure:
                return failure, runtime.closed
            return cleanup_failure, runtime.closed
        return failure, runtime.closed

    def _fail_closed(self) -> None:
        with self._lock:
            failures = tuple(self._failures)
            failure: BaseException
            if len(failures) == 1:
                failure = failures[0]
            else:
                failure = BaseExceptionGroup(
                    "queue worker restart budget exhausted",
                    failures,
                )
            self._failure = failure
            self._state = WorkerSupervisorState.FAILED
        raise failure.with_traceback(failure.__traceback__)

    def _mark_stopped(self) -> None:
        with self._lock:
            self._state = WorkerSupervisorState.STOPPED
