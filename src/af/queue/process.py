"""Spawn-based process boundary for supervised queue-worker runtimes."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import timedelta
from enum import StrEnum
from multiprocessing import get_context
from signal import SIGINT, SIGTERM, getsignal, signal
from threading import RLock
from types import FrameType
from typing import Literal, Protocol, cast, runtime_checkable

from af.framework.errors import PlatformReason
from af.queue.contracts import QueueOperationError
from af.queue.runtime import WorkerStopSignal, WorkerSupervisor

__all__ = (
    "SpawnWorkerProcess",
    "WorkerProcessState",
    "WorkerProcessSupervisor",
    "WorkerProcessSupervisorState",
    "WorkerSignalStopAdapter",
)


@runtime_checkable
class _ProcessHandle(Protocol):
    @property
    def pid(self) -> int | None: ...

    @property
    def exitcode(self) -> int | None: ...

    def start(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...

    def is_alive(self) -> bool: ...


@runtime_checkable
class _ProcessContext(Protocol):
    def get_start_method(self) -> str: ...

    def Event(self) -> WorkerStopSignal: ...

    def Process(self, **kwargs: object) -> _ProcessHandle: ...


class WorkerProcessState(StrEnum):
    """Lifecycle states for one spawned worker process."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class WorkerProcessSupervisorState(StrEnum):
    """Lifecycle states for a parent-level child-process supervisor."""

    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class WorkerSignalStopAdapter:
    """Translate process termination signals into one cooperative stop capability."""

    def __init__(
        self,
        stop_signal: WorkerStopSignal,
        *,
        signals: tuple[int, ...] = (SIGTERM, SIGINT),
        get_handler: Callable[[int], object] = getsignal,
        set_handler: Callable[[int, object], object] | None = None,
    ) -> None:
        if not isinstance(stop_signal, WorkerStopSignal):
            raise TypeError("worker signal adapter requires a WorkerStopSignal")
        if not signals or any(not isinstance(item, int) or item <= 0 for item in signals):
            raise ValueError("worker signal adapter requires positive signal numbers")
        if len(set(signals)) != len(signals):
            raise ValueError("worker signal adapter signals must be unique")
        if not callable(get_handler):
            raise TypeError("worker signal adapter get_handler must be callable")
        if set_handler is not None and not callable(set_handler):
            raise TypeError("worker signal adapter set_handler must be callable")
        self._stop_signal = stop_signal
        self._signals = signals
        self._get_handler = get_handler
        self._set_handler = (
            cast(Callable[[int, object], object], signal) if set_handler is None else set_handler
        )
        self._previous: list[tuple[int, object]] = []
        self._active = False

    def __enter__(self) -> WorkerSignalStopAdapter:
        if self._active:
            raise RuntimeError("worker signal adapter is already active")
        self._active = True
        try:
            for signal_number in self._signals:
                previous = self._get_handler(signal_number)
                self._set_handler(signal_number, self._handle)
                self._previous.append((signal_number, previous))
        except BaseException as install_failure:
            restoration_failures = self._restore()
            if restoration_failures:
                raise BaseExceptionGroup(
                    "worker signal installation and rollback failed",
                    (install_failure, *restoration_failures),
                ) from None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> Literal[False]:
        del exc_type, traceback
        restoration_failures = self._restore()
        if restoration_failures:
            if exc_value is not None:
                raise BaseExceptionGroup(
                    "worker process and signal restoration failed",
                    (exc_value, *restoration_failures),
                ) from None
            if len(restoration_failures) == 1:
                failure = restoration_failures[0]
                raise failure.with_traceback(failure.__traceback__)
            raise BaseExceptionGroup(
                "worker signal restoration failed",
                tuple(restoration_failures),
            ) from None
        return False

    def _handle(self, signal_number: int, frame: FrameType | None) -> None:
        del signal_number, frame
        self._stop_signal.set()

    def _restore(self) -> list[BaseException]:
        failures: list[BaseException] = []
        for signal_number, previous in reversed(self._previous):
            try:
                self._set_handler(signal_number, previous)
            except BaseException as error:
                failures.append(error)
        self._previous.clear()
        self._active = False
        return failures


def _run_spawned_worker(
    supervisor_factory: Callable[[], WorkerSupervisor],
    stop_signal: WorkerStopSignal,
) -> None:
    supervisor = supervisor_factory()
    if not isinstance(supervisor, WorkerSupervisor):
        raise TypeError("worker process supervisor factory must return a WorkerSupervisor")
    with WorkerSignalStopAdapter(stop_signal):
        supervisor.run(stop_event=stop_signal)


class SpawnWorkerProcess:
    """Own one non-daemon spawn child with cooperative shutdown and exit evidence."""

    def __init__(
        self,
        supervisor_factory: Callable[[], WorkerSupervisor],
        *,
        name: str = "af-queue-worker-process",
        process_context: _ProcessContext | None = None,
    ) -> None:
        if not callable(supervisor_factory):
            raise TypeError("worker process supervisor factory must be callable")
        if inspect.iscoroutinefunction(supervisor_factory) or inspect.isasyncgenfunction(
            supervisor_factory
        ):
            raise TypeError("worker process supervisor factory must be synchronous")
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError("worker process name must contain 1 to 128 characters")
        context = get_context("spawn") if process_context is None else process_context
        if not isinstance(context, _ProcessContext):
            raise TypeError("worker process requires a multiprocessing context")
        if context.get_start_method() != "spawn":
            raise ValueError("worker process context must use spawn")
        self._supervisor_factory = supervisor_factory
        self._name = name.strip()
        self._process_context = context
        self._stop_signal: WorkerStopSignal | None = None
        self._process: _ProcessHandle | None = None
        self._started = False
        self._state = WorkerProcessState.IDLE
        self._failure: BaseException | None = None
        self._lock = RLock()

    @property
    def state(self) -> WorkerProcessState:
        with self._lock:
            return self._state

    @property
    def failure(self) -> BaseException | None:
        with self._lock:
            return self._failure

    @property
    def pid(self) -> int | None:
        with self._lock:
            return None if self._process is None else self._process.pid

    @property
    def exit_code(self) -> int | None:
        with self._lock:
            return None if self._process is None else self._process.exitcode

    @property
    def alive(self) -> bool:
        with self._lock:
            process = self._process
        return process is not None and process.is_alive()

    def start(self) -> None:
        with self._lock:
            if self._state is not WorkerProcessState.IDLE:
                raise QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="queue.process.start",
                )
        stop_signal: WorkerStopSignal | None = None
        try:
            stop_signal = self._process_context.Event()
            if not isinstance(stop_signal, WorkerStopSignal):
                raise TypeError("worker process stop signal factory must return a WorkerStopSignal")
            process = self._process_context.Process(
                target=_run_spawned_worker,
                args=(self._supervisor_factory, stop_signal),
                name=self._name,
                daemon=False,
            )
            if not isinstance(process, _ProcessHandle):
                raise TypeError("worker process factory must return a process handle")
            with self._lock:
                self._stop_signal = stop_signal
                self._process = process
            process.start()
        except BaseException as start_failure:
            cleanup_failures: list[BaseException] = []
            if stop_signal is not None:
                try:
                    stop_signal.set()
                except BaseException as cleanup_failure:
                    cleanup_failures.append(cleanup_failure)
            failure = (
                start_failure
                if not cleanup_failures
                else BaseExceptionGroup(
                    "worker process startup and cleanup failed",
                    (start_failure, *cleanup_failures),
                )
            )
            with self._lock:
                self._failure = failure
                self._state = WorkerProcessState.FAILED
            raise failure.with_traceback(failure.__traceback__) from None
        with self._lock:
            self._started = True
            self._state = WorkerProcessState.RUNNING

    def wait(self, *, timeout: timedelta | None = None) -> bool:
        seconds = self._timeout_seconds(timeout)
        with self._lock:
            process = self._process
            started = self._started
            failure = self._failure
        if not started or process is None:
            if failure is not None:
                raise failure.with_traceback(failure.__traceback__)
            raise QueueOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="queue.process.wait",
            )
        process.join(timeout=seconds)
        if process.is_alive():
            return False
        self._record_exit(process)
        return True

    def close(self, *, timeout: timedelta | None = None) -> None:
        seconds = self._timeout_seconds(timeout)
        with self._lock:
            if self._state is WorkerProcessState.IDLE:
                self._state = WorkerProcessState.STOPPED
                return
            process = self._process
            stop_signal = self._stop_signal
            started = self._started
            failure = self._failure
        if not started or process is None or stop_signal is None:
            if failure is not None:
                raise failure.with_traceback(failure.__traceback__)
            return
        if process.is_alive():
            try:
                stop_signal.set()
            except BaseException as stop_failure:
                with self._lock:
                    self._failure = stop_failure
                    self._state = WorkerProcessState.FAILED
                raise
            with self._lock:
                self._state = WorkerProcessState.STOPPING
        process.join(timeout=seconds)
        if process.is_alive():
            raise QueueOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="queue.process.shutdown",
            )
        self._record_exit(process)
        with self._lock:
            failure = self._failure
        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)

    @staticmethod
    def _timeout_seconds(timeout: timedelta | None) -> float | None:
        if timeout is not None and timeout <= timedelta(0):
            raise ValueError("worker process timeout must be positive")
        return None if timeout is None else timeout.total_seconds()

    def _record_exit(self, process: _ProcessHandle) -> None:
        with self._lock:
            if self._failure is not None:
                return
            if process.exitcode == 0:
                self._state = WorkerProcessState.STOPPED
                return
            self._failure = QueueOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="queue.process.exit",
            )
            self._state = WorkerProcessState.FAILED


class WorkerProcessSupervisor:
    """Replace dead spawned children within a fixed, no-overlap restart budget."""

    def __init__(
        self,
        process_factory: Callable[[], SpawnWorkerProcess],
        *,
        max_restarts: int = 0,
        restart_delay: timedelta = timedelta(0),
        watch_interval: timedelta = timedelta(milliseconds=100),
        shutdown_timeout: timedelta = timedelta(seconds=30),
    ) -> None:
        if not callable(process_factory):
            raise TypeError("worker process supervisor factory must be callable")
        if inspect.iscoroutinefunction(process_factory) or inspect.isasyncgenfunction(
            process_factory
        ):
            raise TypeError("worker process supervisor factory must be synchronous")
        if type(max_restarts) is not int or max_restarts < 0:
            raise ValueError("worker process restart budget must be non-negative")
        if restart_delay < timedelta(0):
            raise ValueError("worker process restart delay must be non-negative")
        if watch_interval <= timedelta(0):
            raise ValueError("worker process watch interval must be positive")
        if shutdown_timeout <= timedelta(0):
            raise ValueError("worker process shutdown timeout must be positive")
        self._process_factory = process_factory
        self._max_restarts = max_restarts
        self._restart_delay_seconds = restart_delay.total_seconds()
        self._watch_interval = watch_interval
        self._shutdown_timeout = shutdown_timeout
        self._state = WorkerProcessSupervisorState.IDLE
        self._failure: BaseException | None = None
        self._failures: list[BaseException] = []
        self._generation_count = 0
        self._restart_count = 0
        self._lock = RLock()

    @property
    def state(self) -> WorkerProcessSupervisorState:
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

    def run(self, *, stop_event: WorkerStopSignal) -> None:
        if not isinstance(stop_event, WorkerStopSignal):
            raise TypeError("worker process supervisor stop_event must be a WorkerStopSignal")
        with self._lock:
            if self._state is not WorkerProcessSupervisorState.IDLE:
                raise QueueOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="queue.process-supervisor.start",
                )
            self._state = WorkerProcessSupervisorState.RUNNING

        if stop_event.is_set():
            self._mark_stopped()
            return

        while True:
            child: SpawnWorkerProcess | None = None
            try:
                candidate = self._process_factory()
                if not isinstance(candidate, SpawnWorkerProcess):
                    raise TypeError(
                        "worker process supervisor factory must return a SpawnWorkerProcess"
                    )
                child = candidate
                with self._lock:
                    self._generation_count += 1
                child.start()
                while not stop_event.is_set():
                    if child.wait(timeout=self._watch_interval):
                        break

                child_failure = child.failure
                if child_failure is not None:
                    raise child_failure.with_traceback(child_failure.__traceback__)
                if not stop_event.is_set():
                    raise QueueOperationError(
                        reason=PlatformReason.SHUTDOWN_FAILED,
                        stage="queue.process-supervisor.worker-loss",
                    )
                child.close(timeout=self._shutdown_timeout)
            except BaseException as error:
                failure, retired = self._retire_failed_child(
                    child,
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

    @staticmethod
    def _retire_failed_child(
        child: SpawnWorkerProcess | None,
        failure: BaseException,
        *,
        timeout: timedelta,
    ) -> tuple[BaseException, bool]:
        if child is None:
            return failure, True
        try:
            child.close(timeout=timeout)
        except BaseException as cleanup_failure:
            if cleanup_failure is failure:
                return failure, not child.alive
            return cleanup_failure, not child.alive
        return failure, not child.alive

    def _fail_closed(self) -> None:
        with self._lock:
            failures = tuple(self._failures)
            failure: BaseException
            if len(failures) == 1:
                failure = failures[0]
            else:
                failure = BaseExceptionGroup(
                    "queue worker process restart budget exhausted",
                    failures,
                )
            self._failure = failure
            self._state = WorkerProcessSupervisorState.FAILED
        raise failure.with_traceback(failure.__traceback__)

    def _mark_stopped(self) -> None:
        with self._lock:
            self._state = WorkerProcessSupervisorState.STOPPED
