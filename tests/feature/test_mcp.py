from __future__ import annotations

import json

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

from af.bootstrap import create_application
from af.config import Settings
from af.framework.errors import InvalidProviderConfiguration
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
    "af_route_agent",
}

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "1.0"},
    },
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
            assert tools["af_route_agent"].annotations is not None
            assert tools["af_route_agent"].annotations.readOnlyHint is True
            assert tools["af_route_agent"].annotations.idempotentHint is True
            route_schema = json.dumps(tools["af_route_agent"].outputSchema)
            for value in ("architect", "builder", "reviewer", "tester", "general"):
                assert value in route_schema
            for value in ("routed", "handoff", "ambiguous"):
                assert value in route_schema

            routed = await session.call_tool("af_route_agent", {"prompt": "Implement the endpoint"})
            assert routed.isError is False
            assert routed.structuredContent == {
                "prompt": "Implement the endpoint",
                "status": "routed",
                "agent": "builder",
                "reason": "specialist_match",
                "candidates": ["builder"],
            }

            ambiguous = await session.call_tool("af_route_agent", {"prompt": "Review the tests"})
            assert ambiguous.isError is False
            assert ambiguous.structuredContent is not None
            assert ambiguous.structuredContent["status"] == "handoff"
            assert ambiguous.structuredContent["agent"] is None
            assert ambiguous.structuredContent["candidates"] == ["reviewer", "tester"]

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
            assert manifest["agent_roles"] == [
                "architect",
                "builder",
                "general",
                "reviewer",
                "tester",
            ]

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
                ("af_route_agent", {"prompt": "   "}, "prompt must not be blank"),
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


def test_streamable_http_mcp_requires_explicit_transport_trust_for_non_loopback_hosts() -> None:
    application = create_application(
        settings=Settings(host="0.0.0.0", mcp_transport="streamable-http"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    with pytest.raises(InvalidProviderConfiguration):
        create_mcp_server(application)


def test_streamable_http_mcp_keeps_loopback_default_and_accepts_explicit_transport_trust() -> None:
    loopback = create_application(
        settings=Settings(host="127.0.0.1", mcp_transport="streamable-http"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    loopback_server = create_mcp_server(loopback)
    assert loopback_server.settings.transport_security is not None
    assert "127.0.0.1:*" in loopback_server.settings.transport_security.allowed_hosts

    with TestClient(
        loopback_server.streamable_http_app(), base_url="http://127.0.0.1:8000"
    ) as client:
        loopback_response = client.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={
                "accept": "application/json",
                "host": "127.0.0.1:8000",
                "origin": "http://127.0.0.1:8000",
            },
        )

    assert loopback_response.status_code == 200
    assert loopback_response.json()["result"]["serverInfo"]["name"] == "AF Agent Framework"

    lan = create_application(
        settings=Settings(
            host="0.0.0.0",
            mcp_transport="streamable-http",
            allowed_hosts=("af.internal",),
            allowed_origins=("http://af.internal:8000",),
        ),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    lan_server = create_mcp_server(lan)
    assert lan_server.settings.transport_security is not None
    assert "af.internal:8000" in lan_server.settings.transport_security.allowed_hosts
    assert lan_server.settings.transport_security.allowed_origins == ["http://af.internal:8000"]

    with TestClient(lan_server.streamable_http_app(), base_url="http://af.internal:8000") as client:
        allowed = client.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={
                "accept": "application/json",
                "host": "af.internal:8000",
                "origin": "http://af.internal:8000",
            },
        )
        bad_host = client.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={
                "accept": "application/json",
                "host": "evil.invalid:8000",
                "origin": "http://af.internal:8000",
            },
        )
        bad_origin = client.post(
            "/mcp",
            json=INITIALIZE_REQUEST,
            headers={
                "accept": "application/json",
                "host": "af.internal:8000",
                "origin": "http://evil.invalid",
            },
        )

    assert allowed.status_code == 200
    assert allowed.json()["result"]["serverInfo"]["name"] == "AF Agent Framework"
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403


def test_streamable_http_mcp_rejects_invalid_explicit_transport_entries() -> None:
    for allowed_hosts, allowed_origins in (
        (("*",), ("http://af.internal:8000",)),
        (("af.internal/path",), ("http://af.internal:8000",)),
        (("af.internal",), ("http://af.internal/path",)),
        (("af.internal",), ("http://*.invalid",)),
        (("af.internal",), ("http://af.internal:",)),
    ):
        application = create_application(
            settings=Settings(
                host="0.0.0.0",
                mcp_transport="streamable-http",
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
            ),
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
        )

        with pytest.raises(InvalidProviderConfiguration):
            create_mcp_server(application)


def test_streamable_http_mcp_validates_explicit_transport_entries_on_loopback_hosts() -> None:
    for allowed_hosts, allowed_origins in (
        (("af.internal/path",), ()),
        ((), ("http://af.internal:",)),
    ):
        application = create_application(
            settings=Settings(
                host="127.0.0.1",
                mcp_transport="streamable-http",
                allowed_hosts=allowed_hosts,
                allowed_origins=allowed_origins,
            ),
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
        )

        with pytest.raises(InvalidProviderConfiguration):
            create_mcp_server(application)


def test_streamable_http_mcp_canonicalizes_default_port_origins() -> None:
    application = create_application(
        settings=Settings(
            host="0.0.0.0",
            port=80,
            mcp_transport="streamable-http",
            allowed_hosts=("af.internal",),
            allowed_origins=("http://af.internal:80", "http://af.internal"),
        ),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    server = create_mcp_server(application)

    assert server.settings.transport_security is not None
    assert server.settings.transport_security.allowed_origins == ["http://af.internal"]
