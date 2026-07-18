from __future__ import annotations

import json

import anyio
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from af.bootstrap import create_application
from af.framework.routing import ProcedureRegistry
from af.framework.types import PROMPT_MAX_LENGTH
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository
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

            manifest_result = await session.read_resource("af://manifest")
            manifest = json.loads(manifest_result.contents[0].text)
            assert manifest["version"].endswith("a0")
            assert manifest["routes"] == ["character-count", "hello"]
            assert manifest["tiers"] == ["procedural", "static"]
            assert "generative" not in manifest["tiers"]

    anyio.run(scenario)


class ExplodingSelector:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "mcp-test-selector"

    @property
    def locality(self):  # type: ignore[no-untyped-def]
        from af.framework.types import SelectorLocality

        return SelectorLocality.LOCAL

    def select(self, prompt, catalogue):  # type: ignore[no-untyped-def]
        self.calls += 1
        raise AssertionError(f"selector must not run for typed MCP count: {prompt} / {catalogue!r}")


def test_mcp_count_characters_uses_typed_route_without_selector() -> None:
    selector = ExplodingSelector()
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )

    async def scenario() -> None:
        server = create_mcp_server(application)
        async with create_connected_server_and_client_session(server) as session:
            cases = [
                ("   ", " ", "3"),
                ("version2", "2", "1"),
                ("ends?", "?", "1"),
                ("line 1\nline 2", "\n", "1"),
                ("mañana", "ñ", "1"),
                ("Straße", "s", "1"),
                ("Straße", "ß", "1"),
                ("a" * PROMPT_MAX_LENGTH, "a", str(PROMPT_MAX_LENGTH)),
            ]

            for text, character, expected in cases:
                counted = await session.call_tool(
                    "af_count_characters",
                    {"text": text, "character": character},
                )

                assert counted.isError is False
                assert counted.structuredContent is not None
                assert counted.structuredContent["prompt"] == text
                assert counted.structuredContent["output"] == expected
                trace = application.traces.get(str(counted.structuredContent["trace_id"]))
                assert trace.prompt == text
                assert trace.arguments == {"text": text, "character": character}

            too_long = await session.call_tool(
                "af_count_characters",
                {"text": "a" * (PROMPT_MAX_LENGTH + 1), "character": "a"},
            )
            assert too_long.isError is True
            assert too_long.content[0].type == "text"
            assert (
                f"prompt must be at most {PROMPT_MAX_LENGTH} characters" in too_long.content[0].text
            )

            invalid_character = await session.call_tool(
                "af_count_characters",
                {"text": "strawberry", "character": "rr"},
            )
            assert invalid_character.isError is True
            assert invalid_character.content[0].type == "text"
            assert "exactly one code point" in invalid_character.content[0].text

    anyio.run(scenario)

    assert selector.calls == 0


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


def test_mcp_sanitizes_unexpected_tool_failures() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    def explode(_: str | None = None) -> None:
        raise RuntimeError("SECRET-database-url")

    application.tasks.list = lambda: explode()  # type: ignore[assignment]
    application.tasks.create = lambda prompt: explode(prompt)  # type: ignore[assignment]
    application.tasks.execute = lambda prompt: explode(prompt)  # type: ignore[assignment]

    async def scenario() -> None:
        server = create_mcp_server(application)
        async with create_connected_server_and_client_session(server) as session:
            for tool_name, arguments in (
                ("af_list_tasks", {}),
                ("af_create_task", {"prompt": "how many rs in strawberry"}),
                ("af_hello", {}),
            ):
                result = await session.call_tool(tool_name, arguments)
                serialized = json.dumps(result.model_dump())

                assert result.isError is True
                assert result.structuredContent is None
                assert "SECRET" not in serialized
                assert result.content[0].type == "text"
                assert "MCP_INTERNAL_ERROR" in result.content[0].text
                assert "SECRET" not in result.content[0].text

    anyio.run(scenario)


def test_mcp_sanitizes_unexpected_resource_failures() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    registry = application.container.resolve(ProcedureRegistry)
    registry.catalogue = lambda: (_ for _ in ()).throw(RuntimeError("SECRET-manifest"))  # type: ignore[assignment]

    async def scenario() -> None:
        server = create_mcp_server(application)
        async with create_connected_server_and_client_session(server) as session:
            with pytest.raises(Exception) as error_info:
                await session.read_resource("af://manifest")

        message = str(error_info.value)
        assert "SECRET" not in message
        assert "MCP_INTERNAL_ERROR" in message

    anyio.run(scenario)
