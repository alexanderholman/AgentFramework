from __future__ import annotations

import anyio
from mcp.shared.memory import create_connected_server_and_client_session

from af.framework.types import PROMPT_MAX_LENGTH
from af.interfaces.mcp import create_mcp_server

EXPECTED_TOOLS = {
    "af_hello",
    "af_ask",
    "af_count_characters",
    "af_list_tasks",
    "af_get_task",
    "af_create_task",
    "af_update_task",
    "af_delete_task",
}


def test_mcp_catalogue_annotations_and_task_crud(application) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        server = create_mcp_server(application)
        async with create_connected_server_and_client_session(server) as session:
            catalogue = await session.list_tools()
            tools = {tool.name: tool for tool in catalogue.tools}
            assert set(tools) == EXPECTED_TOOLS
            assert tools["af_hello"].annotations is not None
            assert tools["af_hello"].annotations.readOnlyHint is False
            assert tools["af_hello"].annotations.idempotentHint is False
            assert tools["af_list_tasks"].annotations is not None
            assert tools["af_list_tasks"].annotations.readOnlyHint is True
            assert tools["af_ask"].annotations is not None
            assert tools["af_ask"].annotations.readOnlyHint is False
            assert tools["af_delete_task"].annotations is not None
            assert tools["af_delete_task"].annotations.destructiveHint is True

            asked = await session.call_tool("af_ask", {"prompt": "how many rs in strawberry"})
            assert asked.isError is False
            assert asked.structuredContent is not None
            assert asked.structuredContent["output"] == "3"
            assert asked.structuredContent["verified"] is True

            counted = await session.call_tool(
                "af_count_characters", {"text": "blueberry", "character": "b"}
            )
            assert counted.structuredContent is not None
            assert counted.structuredContent["output"] == "2"

            created = await session.call_tool(
                "af_create_task", {"prompt": "how many rs in strawberry"}
            )
            assert created.structuredContent is not None
            task_id = str(created.structuredContent["id"])

            fetched = await session.call_tool("af_get_task", {"task_id": task_id})
            assert fetched.structuredContent == created.structuredContent

            listed = await session.call_tool("af_list_tasks", {})
            assert listed.structuredContent is not None
            assert listed.structuredContent["total"] == 1

            updated = await session.call_tool(
                "af_update_task",
                {"task_id": task_id, "prompt": "how many ps in pineapple"},
            )
            assert updated.structuredContent is not None
            assert updated.structuredContent["output"] == "3"

            deleted = await session.call_tool("af_delete_task", {"task_id": task_id})
            assert deleted.structuredContent == {"id": task_id, "deleted": True}

            resources = await session.list_resources()
            assert [str(resource.uri) for resource in resources.resources] == ["af://manifest"]

    anyio.run(scenario)


def test_mcp_reports_domain_failures_as_tool_errors(application) -> None:  # type: ignore[no-untyped-def]
    async def scenario() -> None:
        server = create_mcp_server(application)
        async with create_connected_server_and_client_session(server) as session:
            cases = [
                (
                    "af_count_characters",
                    {"text": "strawberry", "character": "rr"},
                    "character must contain exactly one code point, got 'rr'",
                ),
                ("af_ask", {"prompt": "   "}, "prompt must not be blank"),
                (
                    "af_create_task",
                    {"prompt": "make tea"},
                    "no route found for prompt: 'make tea'",
                ),
                (
                    "af_ask",
                    {"prompt": "a" * (PROMPT_MAX_LENGTH + 1)},
                    f"prompt must be at most {PROMPT_MAX_LENGTH} characters",
                ),
                ("af_get_task", {"task_id": "missing"}, "task not found: missing"),
                (
                    "af_update_task",
                    {"task_id": "missing", "prompt": "how many rs in strawberry"},
                    "task not found: missing",
                ),
                ("af_delete_task", {"task_id": "missing"}, "task not found: missing"),
            ]

            for tool_name, arguments, expected_error in cases:
                result = await session.call_tool(tool_name, arguments)

                assert result.isError is True
                assert result.structuredContent is None
                assert len(result.content) == 1
                assert result.content[0].type == "text"
                assert expected_error in result.content[0].text

            listed = await session.call_tool("af_list_tasks", {})
            assert listed.isError is False
            assert listed.structuredContent is not None
            assert listed.structuredContent["total"] == 0
            assert listed.structuredContent["items"] == []

    anyio.run(scenario)
