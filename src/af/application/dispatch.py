"""Command/query messages and compatibility façades for application use cases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from threading import Thread
from typing import Any, cast

from af.application.agent_routing import AgentRoutingController
from af.application.bus import (
    ApplicationBus,
    ApplicationResult,
    Command,
    HandlerRegistry,
    Message,
    Middleware,
    Query,
)
from af.application.ideons import IdeonController, InterpretRequest
from af.application.operations import OperationsController, OperationsSnapshot
from af.application.tasks import TaskController
from af.domain.agent_routing import AgentRouteResult
from af.domain.ideons import IdeonGraph, IdeonRef
from af.domain.ideons.records import InterpretationRecord
from af.domain.tasks import Task
from af.framework.context import (
    AcceptanceScope,
    ExecutionContext,
    maybe_current_context,
)
from af.framework.types import ExecutionOutcome

__all__ = (
    "ApplicationEndpoints",
    "CountCharacters",
    "CreateTask",
    "DeleteTask",
    "ExecuteTask",
    "GetOperationsSnapshot",
    "GetTask",
    "InterpretIdeon",
    "ListTasks",
    "RealiseIdeon",
    "ResolveIdeon",
    "RouteAgent",
    "StreamIdeon",
    "UpdateTask",
    "build_application_endpoints",
)

type ContextFactory = Callable[[], ExecutionContext]


@dataclass(frozen=True, slots=True)
class ExecuteTask(Command):
    prompt: str


@dataclass(frozen=True, slots=True)
class CreateTask(Command):
    prompt: str


@dataclass(frozen=True, slots=True)
class GetTask(Query):
    task_id: str


@dataclass(frozen=True, slots=True)
class ListTasks(Query):
    limit: int | None = None
    offset: int = 0


@dataclass(frozen=True, slots=True)
class GetOperationsSnapshot(Query):
    pass


@dataclass(frozen=True, slots=True)
class UpdateTask(Command):
    task_id: str
    prompt: str


@dataclass(frozen=True, slots=True)
class CountCharacters(Command):
    text: str
    character: str


@dataclass(frozen=True, slots=True)
class DeleteTask(Command):
    task_id: str


@dataclass(frozen=True, slots=True)
class RouteAgent(Query):
    prompt: object


@dataclass(frozen=True, slots=True)
class InterpretIdeon(Query):
    request: InterpretRequest


@dataclass(frozen=True, slots=True)
class ResolveIdeon(Query):
    ref: IdeonRef


@dataclass(frozen=True, slots=True)
class RealiseIdeon(Query):
    ref: IdeonRef


@dataclass(frozen=True, slots=True)
class StreamIdeon(Query):
    request: InterpretRequest


def _default_context() -> ExecutionContext:
    current = maybe_current_context()
    if current is not None:
        return current
    return ExecutionContext(
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )


def _run_sync[T](operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(operation())

    values: list[T] = []
    failures: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            values.append(asyncio.run(operation()))
        except BaseException as error:
            failures.append(error)

    worker = Thread(target=run_in_thread, name="af-application-dispatch")
    worker.start()
    worker.join()
    if failures:
        raise failures[0]
    return values[0]


class _SyncDispatcher:
    def __init__(self, bus: ApplicationBus, context_factory: ContextFactory) -> None:
        self._bus = bus
        self._context_factory = context_factory

    def dispatch(self, message: object) -> object:
        context = self._context_factory()

        async def invoke() -> object:
            result = await self._bus.dispatch(message, context=context)
            return result.value

        return _run_sync(invoke)


class _TaskControllerFacade(TaskController):
    def __init__(self, dispatcher: _SyncDispatcher) -> None:
        self._dispatcher = dispatcher

    def execute(self, prompt: str) -> ExecutionOutcome:
        return cast(ExecutionOutcome, self._dispatcher.dispatch(ExecuteTask(prompt)))

    def create(self, prompt: str) -> Task:
        return cast(Task, self._dispatcher.dispatch(CreateTask(prompt)))

    def get(self, task_id: str) -> Task:
        return cast(Task, self._dispatcher.dispatch(GetTask(task_id)))

    def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        return cast(list[Task], self._dispatcher.dispatch(ListTasks(limit, offset)))

    def update(self, task_id: str, prompt: str) -> Task:
        return cast(Task, self._dispatcher.dispatch(UpdateTask(task_id, prompt)))

    def count_characters(self, text: str, character: str) -> ExecutionOutcome:
        return cast(
            ExecutionOutcome,
            self._dispatcher.dispatch(CountCharacters(text, character)),
        )

    def delete(self, task_id: str) -> None:
        self._dispatcher.dispatch(DeleteTask(task_id))


class _AgentRoutingControllerFacade(AgentRoutingController):
    def __init__(self, dispatcher: _SyncDispatcher) -> None:
        self._dispatcher = dispatcher

    def route(self, prompt: object) -> AgentRouteResult:
        return cast(AgentRouteResult, self._dispatcher.dispatch(RouteAgent(prompt)))


class _OperationsControllerFacade(OperationsController):
    def __init__(self, dispatcher: _SyncDispatcher) -> None:
        self._dispatcher = dispatcher

    def snapshot(self) -> OperationsSnapshot:
        return cast(OperationsSnapshot, self._dispatcher.dispatch(GetOperationsSnapshot()))


class _IdeonControllerFacade(IdeonController):
    def __init__(self, dispatcher: _SyncDispatcher) -> None:
        self._dispatcher = dispatcher

    def interpret(self, request: InterpretRequest) -> InterpretationRecord:
        return cast(
            InterpretationRecord,
            self._dispatcher.dispatch(InterpretIdeon(request)),
        )

    def resolve(self, ref: IdeonRef) -> IdeonGraph:
        return cast(IdeonGraph, self._dispatcher.dispatch(ResolveIdeon(ref)))

    def realise(self, ref: IdeonRef) -> str:
        return cast(str, self._dispatcher.dispatch(RealiseIdeon(ref)))

    def stream(self, request: InterpretRequest) -> tuple[bytes, str]:
        return cast(
            tuple[bytes, str],
            self._dispatcher.dispatch(StreamIdeon(request)),
        )


@dataclass(frozen=True, slots=True)
class ApplicationEndpoints:
    bus: ApplicationBus
    tasks: TaskController
    agent_routes: AgentRoutingController
    ideons: IdeonController
    operations: OperationsController | None = None


def _register[M: Message](
    registry: HandlerRegistry,
    message_type: type[M],
    operation: Callable[[M], object],
) -> None:
    def handle(message: Message, context: ExecutionContext) -> ApplicationResult[object]:
        del context
        if not isinstance(message, message_type):
            raise TypeError("application message type does not match its handler")
        return ApplicationResult(operation(message))

    registry.register(message_type, handle)


def build_application_endpoints(
    tasks: TaskController,
    agent_routes: AgentRoutingController,
    ideons: IdeonController,
    operations: OperationsController | None = None,
    *,
    middleware: Iterable[Middleware] = (),
    context_factory: ContextFactory = _default_context,
) -> ApplicationEndpoints:
    """Build one authoritative bus while preserving established controller APIs."""
    registry = HandlerRegistry()
    _register(registry, ExecuteTask, lambda message: tasks.execute(message.prompt))
    _register(registry, CreateTask, lambda message: tasks.create(message.prompt))
    _register(registry, GetTask, lambda message: tasks.get(message.task_id))
    _register(
        registry,
        ListTasks,
        lambda message: tasks.list(limit=message.limit, offset=message.offset),
    )
    _register(
        registry,
        UpdateTask,
        lambda message: tasks.update(message.task_id, message.prompt),
    )
    _register(
        registry,
        CountCharacters,
        lambda message: tasks.count_characters(message.text, message.character),
    )
    _register(registry, DeleteTask, lambda message: tasks.delete(message.task_id))
    _register(registry, RouteAgent, lambda message: agent_routes.route(message.prompt))
    _register(registry, InterpretIdeon, lambda message: ideons.interpret(message.request))
    _register(registry, ResolveIdeon, lambda message: ideons.resolve(message.ref))
    _register(registry, RealiseIdeon, lambda message: ideons.realise(message.ref))
    _register(registry, StreamIdeon, lambda message: ideons.stream(message.request))
    if operations is not None:
        _register(registry, GetOperationsSnapshot, lambda message: operations.snapshot())

    bus = ApplicationBus(registry, middleware=middleware)
    dispatcher = _SyncDispatcher(bus, context_factory)
    return ApplicationEndpoints(
        bus=bus,
        tasks=_TaskControllerFacade(dispatcher),
        agent_routes=_AgentRoutingControllerFacade(dispatcher),
        ideons=_IdeonControllerFacade(dispatcher),
        operations=_OperationsControllerFacade(dispatcher) if operations is not None else None,
    )
