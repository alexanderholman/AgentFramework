from __future__ import annotations

from datetime import timedelta
from signal import SIGINT, SIGTERM
from threading import Event, Thread
from time import monotonic
from typing import Any

import pytest

from af.framework.errors import PlatformReason
from af.queue.contracts import QueueOperationError
from af.queue.process import (
    SpawnWorkerProcess,
    WorkerProcessState,
    WorkerProcessSupervisor,
    WorkerProcessSupervisorState,
    WorkerSignalStopAdapter,
)
from af.queue.runtime import WorkerRuntime, WorkerSupervisor


class _StopSignal:
    def __init__(self) -> None:
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def set(self) -> None:
        self._set = True

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        return self._set


class _Process:
    def __init__(self, **kwargs: Any) -> None:
        self.target = kwargs["target"]
        self.args = kwargs["args"]
        self.name = kwargs["name"]
        self.daemon = kwargs["daemon"]
        self.pid: int | None = None
        self.exitcode: int | None = None
        self.alive = False
        self.allow_cooperative_exit = True
        self.exit_on_start: int | None = None
        self.raise_after_start: BaseException | None = None
        self.joins: list[float | None] = []

    def start(self) -> None:
        self.pid = 321
        self.alive = True
        if self.exit_on_start is not None:
            self.crash(self.exit_on_start)
        if self.raise_after_start is not None:
            raise self.raise_after_start

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)
        stop_signal = self.args[1]
        if self.allow_cooperative_exit and stop_signal.is_set():
            self.alive = False
            self.exitcode = 0

    def is_alive(self) -> bool:
        return self.alive

    def crash(self, exit_code: int) -> None:
        self.alive = False
        self.exitcode = exit_code


class _ProcessContext:
    def __init__(
        self,
        *,
        start_method: str = "spawn",
        exit_on_start: int | None = None,
        raise_after_start: BaseException | None = None,
    ) -> None:
        self.start_method = start_method
        self.exit_on_start = exit_on_start
        self.raise_after_start = raise_after_start
        self.signals: list[_StopSignal] = []
        self.processes: list[_Process] = []

    def get_start_method(self) -> str:
        return self.start_method

    def Event(self) -> _StopSignal:
        signal = _StopSignal()
        self.signals.append(signal)
        return signal

    def Process(self, **kwargs: Any) -> _Process:
        process = _Process(**kwargs)
        process.exit_on_start = self.exit_on_start
        process.raise_after_start = self.raise_after_start
        self.processes.append(process)
        return process


class _BlockingProcessContext(_ProcessContext):
    def __init__(self) -> None:
        super().__init__()
        self.event_factory_entered = Event()
        self.release_event_factory = Event()

    def Event(self) -> _StopSignal:
        self.event_factory_entered.set()
        assert self.release_event_factory.wait(timeout=1)
        return super().Event()


class _SpawnKernel:
    max_concurrency = 1

    def run_loop(self, handler: object, **kwargs: Any) -> None:
        del handler
        kwargs["stop_event"].wait()

    def start_draining(self) -> None:
        return None

    def close(self, *, timeout: timedelta | None = None) -> None:
        del timeout


def _spawn_runtime_factory() -> WorkerRuntime:
    return WorkerRuntime(_SpawnKernel(), lambda job: None)


def _spawn_supervisor_factory() -> WorkerSupervisor:
    return WorkerSupervisor(
        _spawn_runtime_factory,
        watch_interval=timedelta(milliseconds=5),
        shutdown_timeout=timedelta(seconds=1),
    )


def _process_harness(
    supervisor_factory: Any,
) -> tuple[SpawnWorkerProcess, list[_StopSignal], list[_Process]]:
    context = _ProcessContext()
    worker = SpawnWorkerProcess(
        supervisor_factory,
        name="af-test-worker",
        process_context=context,
    )
    return worker, context.signals, context.processes


def _wait_until(predicate: Any, *, timeout: float = 1.0) -> bool:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return True
        Event().wait(0.005)
    return bool(predicate())


def test_spawn_worker_process_rejects_non_spawn_context() -> None:
    with pytest.raises(ValueError, match="spawn"):
        SpawnWorkerProcess(
            lambda: WorkerSupervisor(lambda: pytest.fail("not executed")),
            process_context=_ProcessContext(start_method="fork"),
        )


def test_spawn_worker_process_composes_supervisor_only_inside_child() -> None:
    composition_calls = 0

    def supervisor_factory() -> WorkerSupervisor:
        nonlocal composition_calls
        composition_calls += 1
        return WorkerSupervisor(lambda: pytest.fail("stopped child composed a runtime"))

    worker, signals, processes = _process_harness(supervisor_factory)

    worker.start()

    assert composition_calls == 0
    assert len(signals) == 1
    assert len(processes) == 1
    process = processes[0]
    assert process.name == "af-test-worker"
    assert process.daemon is False
    assert process.pid == 321
    assert worker.pid == 321
    assert worker.state is WorkerProcessState.RUNNING

    signals[0].set()
    process.target(*process.args)
    assert composition_calls == 1


def test_spawn_worker_process_allows_only_one_concurrent_start() -> None:
    context = _BlockingProcessContext()
    worker = SpawnWorkerProcess(
        lambda: WorkerSupervisor(lambda: pytest.fail("fake child target not executed")),
        process_context=context,
    )
    failures: list[BaseException] = []

    def start() -> None:
        try:
            worker.start()
        except BaseException as error:
            failures.append(error)

    first = Thread(target=start)
    second = Thread(target=start)
    first.start()
    assert context.event_factory_entered.wait(timeout=1)
    second.start()
    context.release_event_factory.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(context.processes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], QueueOperationError)
    assert worker.state is WorkerProcessState.RUNNING


def test_spawn_worker_process_close_waits_for_concurrent_start() -> None:
    context = _BlockingProcessContext()
    worker = SpawnWorkerProcess(
        lambda: WorkerSupervisor(lambda: pytest.fail("fake child target not executed")),
        process_context=context,
    )
    failures: list[BaseException] = []

    def start() -> None:
        try:
            worker.start()
        except BaseException as error:
            failures.append(error)

    starter = Thread(target=start)
    closer = Thread(target=lambda: worker.close(timeout=timedelta(seconds=1)))
    starter.start()
    assert context.event_factory_entered.wait(timeout=1)
    closer.start()
    context.release_event_factory.set()
    starter.join(timeout=1)
    closer.join(timeout=1)

    assert not starter.is_alive()
    assert not closer.is_alive()
    assert failures == []
    assert len(context.processes) == 1
    assert context.signals[0].is_set()
    assert worker.state is WorkerProcessState.STOPPED


def test_spawn_worker_process_close_requests_cooperative_stop_and_joins() -> None:
    worker, signals, processes = _process_harness(
        lambda: WorkerSupervisor(lambda: pytest.fail("not executed by parent"))
    )
    worker.start()

    worker.close(timeout=timedelta(milliseconds=25))

    assert signals[0].is_set()
    assert processes[0].joins == [0.025]
    assert worker.exit_code == 0
    assert worker.failure is None
    assert worker.state is WorkerProcessState.STOPPED


def test_spawn_worker_process_runs_and_stops_real_spawn_child() -> None:
    worker = SpawnWorkerProcess(_spawn_supervisor_factory, name="af-real-spawn-test")

    worker.start()
    assert worker.pid is not None
    worker.close(timeout=timedelta(seconds=3))

    assert worker.exit_code == 0
    assert worker.failure is None
    assert worker.state is WorkerProcessState.STOPPED


def test_spawn_worker_process_close_timeout_is_retryable_without_termination() -> None:
    worker, signals, processes = _process_harness(
        lambda: WorkerSupervisor(lambda: pytest.fail("not executed by parent"))
    )
    worker.start()
    process = processes[0]
    process.allow_cooperative_exit = False

    with pytest.raises(QueueOperationError) as timeout:
        worker.close(timeout=timedelta(milliseconds=10))

    assert timeout.value.reason is PlatformReason.SHUTDOWN_FAILED
    assert timeout.value.stage == "queue.process.shutdown"
    assert signals[0].is_set()
    assert worker.failure is None
    assert worker.state is WorkerProcessState.STOPPING

    process.allow_cooperative_exit = True
    worker.close(timeout=timedelta(milliseconds=20))
    assert process.joins == [0.01, 0.02]
    assert worker.state is WorkerProcessState.STOPPED


def test_spawn_worker_process_surfaces_nonzero_child_exit_without_hiding_code() -> None:
    worker, _, processes = _process_harness(
        lambda: WorkerSupervisor(lambda: pytest.fail("not executed by parent"))
    )
    worker.start()
    processes[0].crash(17)

    assert worker.wait(timeout=timedelta(milliseconds=10))

    failure = worker.failure
    assert isinstance(failure, QueueOperationError)
    assert failure.reason is PlatformReason.SHUTDOWN_FAILED
    assert failure.stage == "queue.process.exit"
    assert worker.exit_code == 17
    assert worker.state is WorkerProcessState.FAILED
    with pytest.raises(QueueOperationError) as sticky:
        worker.close(timeout=timedelta(milliseconds=10))
    assert sticky.value is failure


def test_process_supervisor_restarts_dead_child_without_generation_overlap() -> None:
    contexts = [_ProcessContext(exit_on_start=17), _ProcessContext()]
    factory_calls = 0

    def process_factory() -> SpawnWorkerProcess:
        nonlocal factory_calls
        context = contexts[factory_calls]
        factory_calls += 1
        return SpawnWorkerProcess(
            lambda: WorkerSupervisor(lambda: pytest.fail("fake child target not executed")),
            process_context=context,
        )

    stop = Event()
    supervisor = WorkerProcessSupervisor(
        process_factory,
        max_restarts=1,
        watch_interval=timedelta(milliseconds=5),
    )
    failures: list[BaseException] = []

    def supervise() -> None:
        try:
            supervisor.run(stop_event=stop)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=supervise)
    thread.start()
    try:
        assert _wait_until(lambda: len(contexts[1].processes) == 1)
        assert contexts[0].processes[0].is_alive() is False
        assert contexts[1].processes[0].is_alive() is True
        stop.set()
        thread.join(timeout=1)
        assert not thread.is_alive()

        assert failures == []
        assert factory_calls == 2
        assert supervisor.generation_count == 2
        assert supervisor.restart_count == 1
        assert len(supervisor.failures) == 1
        assert supervisor.state is WorkerProcessSupervisorState.STOPPED
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(timeout=1)


def test_process_supervisor_preserves_failures_when_restart_budget_exhausts() -> None:
    contexts = [_ProcessContext(exit_on_start=7), _ProcessContext(exit_on_start=9)]
    factory_calls = 0

    def process_factory() -> SpawnWorkerProcess:
        nonlocal factory_calls
        context = contexts[factory_calls]
        factory_calls += 1
        return SpawnWorkerProcess(
            lambda: WorkerSupervisor(lambda: pytest.fail("fake child target not executed")),
            process_context=context,
        )

    supervisor = WorkerProcessSupervisor(
        process_factory,
        max_restarts=1,
        watch_interval=timedelta(milliseconds=5),
    )

    with pytest.raises(BaseExceptionGroup) as captured:
        supervisor.run(stop_event=Event())

    assert captured.value.message == "queue worker process restart budget exhausted"
    assert captured.value.exceptions == supervisor.failures
    assert all(isinstance(error, QueueOperationError) for error in supervisor.failures)
    assert all(error.stage == "queue.process.exit" for error in supervisor.failures)
    assert supervisor.failure is captured.value
    assert supervisor.state is WorkerProcessSupervisorState.FAILED
    assert supervisor.generation_count == 2
    assert supervisor.restart_count == 1
    assert factory_calls == 2


def test_process_supervisor_never_restarts_while_failed_start_child_is_alive() -> None:
    startup_failure = RuntimeError("process start failed after child launch")
    context = _ProcessContext(raise_after_start=startup_failure)
    factory_calls = 0

    def process_factory() -> SpawnWorkerProcess:
        nonlocal factory_calls
        factory_calls += 1
        return SpawnWorkerProcess(
            lambda: WorkerSupervisor(lambda: pytest.fail("fake child target not executed")),
            process_context=context,
        )

    supervisor = WorkerProcessSupervisor(process_factory, max_restarts=3)

    with pytest.raises(RuntimeError) as captured:
        supervisor.run(stop_event=Event())

    assert captured.value is startup_failure
    assert context.processes[0].is_alive()
    assert factory_calls == 1
    assert supervisor.generation_count == 1
    assert supervisor.restart_count == 0
    assert supervisor.failures == (startup_failure,)
    assert supervisor.failure is startup_failure
    assert supervisor.state is WorkerProcessSupervisorState.FAILED


def test_process_supervisor_stop_during_restart_delay_prevents_replacement() -> None:
    context = _ProcessContext(exit_on_start=11)
    factory_calls = 0

    def process_factory() -> SpawnWorkerProcess:
        nonlocal factory_calls
        factory_calls += 1
        return SpawnWorkerProcess(
            lambda: WorkerSupervisor(lambda: pytest.fail("fake child target not executed")),
            process_context=context,
        )

    stop = Event()
    supervisor = WorkerProcessSupervisor(
        process_factory,
        max_restarts=2,
        restart_delay=timedelta(seconds=1),
        watch_interval=timedelta(milliseconds=5),
    )
    failures: list[BaseException] = []

    def supervise() -> None:
        try:
            supervisor.run(stop_event=stop)
        except BaseException as error:
            failures.append(error)

    thread = Thread(target=supervise)
    thread.start()
    try:
        assert _wait_until(lambda: len(supervisor.failures) == 1)
        stop.set()
        thread.join(timeout=1)
        assert not thread.is_alive()

        assert failures == []
        assert factory_calls == 1
        assert supervisor.generation_count == 1
        assert supervisor.restart_count == 0
        assert supervisor.state is WorkerProcessSupervisorState.STOPPED
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(timeout=1)


def test_worker_signal_adapter_translates_and_restores_handlers() -> None:
    stop = _StopSignal()
    original = {SIGTERM: object(), SIGINT: object()}
    current = dict(original)
    changes: list[tuple[int, object]] = []

    def get_handler(signal_number: int) -> object:
        return current[signal_number]

    def set_handler(signal_number: int, handler: object) -> object:
        previous = current[signal_number]
        current[signal_number] = handler
        changes.append((signal_number, handler))
        return previous

    with WorkerSignalStopAdapter(
        stop,
        get_handler=get_handler,
        set_handler=set_handler,
    ):
        assert callable(current[SIGTERM])
        assert callable(current[SIGINT])
        current[SIGTERM](SIGTERM, None)  # type: ignore[operator]
        assert stop.is_set()

    assert current == original
    assert [signal_number for signal_number, _ in changes] == [
        SIGTERM,
        SIGINT,
        SIGINT,
        SIGTERM,
    ]


def test_queue_public_api_exports_process_supervision() -> None:
    from af.queue import (
        SpawnWorkerProcess,
        WorkerProcessState,
        WorkerProcessSupervisor,
        WorkerProcessSupervisorState,
        WorkerSignalStopAdapter,
        WorkerStopSignal,
    )

    assert SpawnWorkerProcess is not None
    assert WorkerProcessState is not None
    assert WorkerProcessSupervisor is not None
    assert WorkerProcessSupervisorState is not None
    assert WorkerSignalStopAdapter is not None
    assert WorkerStopSignal is not None
