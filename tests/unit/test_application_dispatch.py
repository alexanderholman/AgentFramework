from __future__ import annotations

import asyncio
from unittest.mock import Mock

from af.application.agent_routing import AgentRoutingController
from af.application.bus import ApplicationResult, DispatchRequest, MessageKind
from af.application.dispatch import (
    ApplicationEndpoints,
    CountCharacters,
    CreateTask,
    DeleteTask,
    ExecuteTask,
    GetOperationsSnapshot,
    GetTask,
    GetTasks,
    InterpretIdeon,
    ListTasks,
    RealiseIdeon,
    ResolveIdeon,
    RouteAgent,
    StreamIdeon,
    UpdateTask,
    build_application_endpoints,
)
from af.application.ideons import IdeonController
from af.application.operations import OperationsController
from af.application.tasks import TaskController
from af.bootstrap import create_application
from af.config import Settings
from af.framework.context import AcceptanceScope, ExecutionContext


def _controllers() -> tuple[Mock, Mock, Mock, Mock]:
    tasks = Mock(spec=TaskController)
    routes = Mock(spec=AgentRoutingController)
    ideons = Mock(spec=IdeonController)
    operations = Mock(spec=OperationsController)
    tasks.execute.return_value = "executed"
    tasks.create.return_value = "created"
    tasks.get.return_value = "found"
    tasks.get_many.return_value = ["found", None]
    tasks.list.return_value = ["listed"]
    tasks.update.return_value = "updated"
    tasks.count_characters.return_value = "counted"
    tasks.delete.return_value = None
    routes.route.return_value = "routed"
    ideons.interpret.return_value = "interpreted"
    ideons.resolve.return_value = "resolved"
    ideons.realise.return_value = "realised"
    ideons.stream.return_value = (b"streamed", "streamed")
    operations.snapshot.return_value = "operations"
    return tasks, routes, ideons, operations


def test_controller_facades_dispatch_every_use_case_through_one_ordered_bus() -> None:
    tasks, routes, ideons, operations = _controllers()
    observed: list[tuple[MessageKind, str, AcceptanceScope | None]] = []

    async def record(
        request: DispatchRequest,
        call_next,
    ) -> ApplicationResult[object]:
        observed.append(
            (request.kind, type(request.message).__name__, request.context.effective_scope)
        )
        return await call_next(request)

    endpoints = build_application_endpoints(
        tasks,
        routes,
        ideons,
        operations,
        middleware=(record,),
    )

    assert endpoints.tasks.execute("run") == "executed"
    assert endpoints.tasks.create("create") == "created"
    assert endpoints.tasks.get("task-1") == "found"
    assert endpoints.tasks.get_many(("task-1", "missing")) == ["found", None]
    assert endpoints.tasks.list(limit=5, offset=2) == ["listed"]
    assert endpoints.tasks.update("task-1", "update") == "updated"
    assert endpoints.tasks.count_characters("text", "t") == "counted"
    assert endpoints.tasks.delete("task-1") is None
    assert endpoints.agent_routes.route("route") == "routed"
    assert endpoints.ideons.interpret("request") == "interpreted"
    assert endpoints.ideons.resolve("ref") == "resolved"
    assert endpoints.ideons.realise("ref") == "realised"
    assert endpoints.ideons.stream("request") == (b"streamed", "streamed")
    assert endpoints.operations is not None
    assert endpoints.operations.snapshot() == "operations"

    assert endpoints.bus.registry.message_types == (
        ExecuteTask,
        CreateTask,
        GetTask,
        GetTasks,
        ListTasks,
        UpdateTask,
        CountCharacters,
        DeleteTask,
        RouteAgent,
        InterpretIdeon,
        ResolveIdeon,
        RealiseIdeon,
        StreamIdeon,
        GetOperationsSnapshot,
    )
    assert observed == [
        (MessageKind.COMMAND, "ExecuteTask", AcceptanceScope.DETERMINISTIC),
        (MessageKind.COMMAND, "CreateTask", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "GetTask", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "GetTasks", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "ListTasks", AcceptanceScope.DETERMINISTIC),
        (MessageKind.COMMAND, "UpdateTask", AcceptanceScope.DETERMINISTIC),
        (MessageKind.COMMAND, "CountCharacters", AcceptanceScope.DETERMINISTIC),
        (MessageKind.COMMAND, "DeleteTask", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "RouteAgent", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "InterpretIdeon", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "ResolveIdeon", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "RealiseIdeon", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "StreamIdeon", AcceptanceScope.DETERMINISTIC),
        (MessageKind.QUERY, "GetOperationsSnapshot", AcceptanceScope.DETERMINISTIC),
    ]


def test_direct_and_controller_dispatch_have_equivalent_semantics() -> None:
    tasks, routes, ideons, operations = _controllers()
    endpoints = build_application_endpoints(tasks, routes, ideons, operations)
    context = ExecutionContext(
        requested_scope=AcceptanceScope.DETERMINISTIC,
        effective_scope=AcceptanceScope.DETERMINISTIC,
    )

    direct = asyncio.run(
        endpoints.bus.dispatch(
            ExecuteTask("run"),
            context=context,
        )
    )

    assert direct.value == endpoints.tasks.execute("run") == "executed"


def test_existing_endpoint_factory_signature_remains_valid_without_operations() -> None:
    tasks, routes, ideons, _ = _controllers()

    endpoints = build_application_endpoints(tasks, routes, ideons)

    assert endpoints.operations is None
    legacy = ApplicationEndpoints(
        endpoints.bus,
        endpoints.tasks,
        endpoints.agent_routes,
        endpoints.ideons,
    )
    assert legacy.operations is None


def test_sync_controller_dispatch_is_safe_inside_an_active_event_loop() -> None:
    tasks, routes, ideons, operations = _controllers()
    endpoints = build_application_endpoints(tasks, routes, ideons, operations)

    async def scenario() -> None:
        assert endpoints.tasks.execute("run") == "executed"

    asyncio.run(scenario())


def test_bootstrap_exposes_bus_backed_endpoints_to_every_transport() -> None:
    application = create_application(settings=Settings(database_url="sqlite+pysqlite:///:memory:"))
    try:
        assert application.tasks.execute("hello").output == "Hello, world!"
        assert application.bus.registry.message_types == (
            ExecuteTask,
            CreateTask,
            GetTask,
            GetTasks,
            ListTasks,
            UpdateTask,
            CountCharacters,
            DeleteTask,
            RouteAgent,
            InterpretIdeon,
            ResolveIdeon,
            RealiseIdeon,
            StreamIdeon,
            GetOperationsSnapshot,
        )
    finally:
        application.shutdown()


def test_sync_facade_preserves_authorized_container_context_across_async_bridge(
    application,
) -> None:
    from af.framework.context import Principal, current_context, maybe_current_context

    tasks, routes, ideons, operations = _controllers()
    tasks.execute.side_effect = lambda _: current_context()
    endpoints = build_application_endpoints(tasks, routes, ideons, operations)
    context = ExecutionContext(principal=Principal("alice", "human"), tenant_id="workspace-a")
    with application.container.invocation_scope(context=context):
        assert endpoints.tasks.execute("inspect identity") is context
        assert current_context() is context
    assert maybe_current_context() is None
