from __future__ import annotations

import json

import anyio
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from af.framework.context import AcceptanceScope, ExecutionContext, Principal, use_context
from af.interfaces.http import create_http_app
from af.interfaces.mcp import create_mcp_server

DATA = json.dumps({"texts": ["strawberry", "raspberry"], "character": "r"})
FIELDS = "runId complete cancelled nodes { id state verified } outputs { node value }"


def test_graphql_start_mcp_resume_and_http_status_share_a_checkpoint(application) -> None:
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        started = client.post(
            "/graphql",
            json={
                "query": (
                    "mutation Start($data: String!) { startWorkflow(data: $data, maxNodes: 1) { "
                )
                + FIELDS
                + " } }",
                "variables": {"data": DATA},
            },
        )
        body = started.json()
        assert "errors" not in body, body
        partial = body["data"]["startWorkflow"]
        assert partial["complete"] is False
        run_id = partial["runId"]

        async def resume():
            async with create_connected_server_and_client_session(
                create_mcp_server(application)
            ) as session:
                result = await session.call_tool("af_resume_workflow", {"run_id": run_id})
                assert result.isError is False, result
                assert result.structuredContent["complete"] is True
                assert result.structuredContent["outputs"][-1]["value"] == {"total": 6}
                read = await session.call_tool("af_get_workflow", {"run_id": run_id})
                assert read.structuredContent == result.structuredContent

        anyio.run(resume)
        inspected = client.post(
            "/graphql",
            json={
                "query": "query Read($id: ID!) { workflowStatus(id: $id) { " + FIELDS + " } }",
                "variables": {"id": run_id},
            },
        ).json()
        assert inspected["data"]["workflowStatus"]["complete"] is True
        http = client.get(f"/api/v1/workflows/{run_id}").json()
        assert http["outputs"] == inspected["data"]["workflowStatus"]["outputs"]
        assert http["run_id"] == run_id


def test_mcp_workflow_catalogue_annotations_cancel_and_input_bounds(application) -> None:
    async def scenario():
        async with create_connected_server_and_client_session(
            create_mcp_server(application)
        ) as session:
            catalogue = {tool.name: tool for tool in (await session.list_tools()).tools}
            assert catalogue["af_get_workflow"].annotations.readOnlyHint is True
            assert catalogue["af_start_workflow"].annotations.idempotentHint is False
            assert catalogue["af_resume_workflow"].annotations.readOnlyHint is False
            assert catalogue["af_cancel_workflow"].annotations.destructiveHint is True
            created = await session.call_tool("af_start_workflow", {"data": DATA, "max_nodes": 1})
            assert created.isError is False, created
            run_id = created.structuredContent["run_id"]
            cancelled = await session.call_tool("af_cancel_workflow", {"run_id": run_id})
            assert cancelled.structuredContent["cancelled"] is True
            resumed = await session.call_tool("af_resume_workflow", {"run_id": run_id})
            assert resumed.structuredContent["cancelled"] is True
            for arguments in (
                {"data": DATA, "max_nodes": 0},
                {"data": DATA, "name": "not.installed"},
                {"data": "x" * 65537},
            ):
                rejected = await session.call_tool("af_start_workflow", arguments)
                assert rejected.isError is True

    anyio.run(scenario)


def test_graphql_workflow_errors_are_typed_and_get_cannot_mutate(application) -> None:
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        failed = client.post(
            "/graphql",
            json={"query": 'mutation { startWorkflow(name: "uninstalled", data: "{}") { runId } }'},
        ).json()
        assert failed["errors"][0]["extensions"]["code"] == "WORKFLOWERROR"
        bad_limit = client.post(
            "/graphql",
            json={"query": 'mutation { startWorkflow(data: "{}", maxNodes: 129) { runId } }'},
        ).json()
        assert bad_limit["errors"][0]["extensions"]["code"] == "BAD_REQUEST"
        forbidden = client.get(
            "/graphql", params={"query": 'mutation { cancelWorkflow(id: "missing") { cancelled } }'}
        )
        assert forbidden.status_code == 405


def test_mcp_checkpoint_inspection_preserves_host_authority(application) -> None:
    async def scenario():
        common = {
            "requested_scope": AcceptanceScope.DETERMINISTIC,
            "effective_scope": AcceptanceScope.DETERMINISTIC,
        }
        with use_context(ExecutionContext(principal=Principal("owner", "human"), **common)):
            async with create_connected_server_and_client_session(
                create_mcp_server(application)
            ) as session:
                created = await session.call_tool(
                    "af_start_workflow", {"data": DATA, "max_nodes": 1}
                )
                assert created.isError is False, created
                run_id = created.structuredContent["run_id"]
        with use_context(ExecutionContext(principal=Principal("other", "human"), **common)):
            async with create_connected_server_and_client_session(
                create_mcp_server(application)
            ) as session:
                rejected = await session.call_tool("af_get_workflow", {"run_id": run_id})
                assert rejected.isError is True
                assert "authority" in rejected.content[0].text

    anyio.run(scenario)


def test_graphql_resume_cancel_and_structured_results(application) -> None:
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:

        def execute(query, variables=None):
            return client.post(
                "/graphql", json={"query": query, "variables": variables or {}}
            ).json()

        first = execute(
            "mutation Start($data: String!) { startWorkflow(data: $data, maxNodes: 1) { runId } }",
            {"data": DATA},
        )
        run_id = first["data"]["startWorkflow"]["runId"]
        resumed = execute(
            "mutation Resume($id: ID!) { "
            "resumeWorkflow(id: $id) { complete outputs { node value } } }",
            {"id": run_id},
        )
        assert resumed["data"]["resumeWorkflow"]["complete"] is True
        assert resumed["data"]["resumeWorkflow"]["outputs"][-1]["value"] == {"total": 6}
        second = execute(
            "mutation Start($data: String!) { startWorkflow(data: $data, maxNodes: 1) { runId } }",
            {"data": DATA},
        )
        run_id = second["data"]["startWorkflow"]["runId"]
        cancelled = execute(
            "mutation Cancel($id: ID!) { cancelWorkflow(id: $id) { cancelled } }", {"id": run_id}
        )
        assert cancelled["data"]["cancelWorkflow"]["cancelled"] is True


def test_graphql_workflow_internal_errors_are_sanitized(application, monkeypatch) -> None:
    from af.application.workflow_control import WorkflowController

    controller = application.container.resolve(WorkflowController)

    def broken(*_):
        raise RuntimeError("private backend token")

    monkeypatch.setattr(controller, "inspect", broken)
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        result = client.post(
            "/graphql", json={"query": '{ workflowStatus(id: "missing") { runId } }'}
        ).json()
        assert result["errors"][0]["extensions"]["code"] == "INTERNAL_ERROR"
        assert "private" not in json.dumps(result)


def test_graphql_workflow_execution_does_not_block_the_event_loop(application, monkeypatch) -> None:
    import asyncio
    from threading import Event

    from af.application.workflow_control import WorkflowController
    from af.interfaces.graphql import schema

    controller = application.container.resolve(WorkflowController)
    original = controller.start
    started, release = Event(), Event()

    def blocking_start(*args, **kwargs):
        started.set()
        if not release.wait(timeout=3):
            raise RuntimeError("GraphQL event loop could not release workflow worker")
        return original(*args, **kwargs)

    monkeypatch.setattr(controller, "start", blocking_start)

    async def scenario():
        context = ExecutionContext(
            requested_scope=AcceptanceScope.DETERMINISTIC,
            effective_scope=AcceptanceScope.DETERMINISTIC,
        )
        task = asyncio.create_task(
            schema.execute(
                "mutation Start($data: String!) { startWorkflow(data: $data) { complete } }",
                variable_values={"data": DATA},
                context_value={
                    "controller": application.tasks,
                    "workflows": controller,
                    "execution_context": context,
                },
            )
        )
        try:
            assert await asyncio.wait_for(asyncio.to_thread(started.wait, 1), timeout=2)
            assert not task.done()
        finally:
            release.set()
        result = await task
        assert result.errors is None
        assert result.data["startWorkflow"]["complete"] is True

    anyio.run(scenario)
