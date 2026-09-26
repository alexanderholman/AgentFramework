from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp.shared.memory import create_connected_server_and_client_session

import af.interfaces.mcp as mcp_module
from af.application.self_design_execution import (
    SelfDesignExecutionController,
    SelfDesignExecutionResult,
)
from af.bootstrap import create_application
from af.config import Settings
from af.framework.application import ApplicationState
from af.framework.container import Container, ProviderRegistrar, ServiceProvider
from af.framework.errors import InvalidProviderConfiguration, PlatformError, PlatformReason
from af.framework.routing import ProcedureRegistry
from af.framework.self_design import DesignCandidate, DesignPolicy, DesignSnapshot, DesignUnitKind
from af.framework.self_design_operation import SelfDesignOperation
from af.framework.self_design_publication import (
    DesignPublicationEffect,
    DesignPublicationReceipt,
    DesignPublicationStatus,
)
from af.framework.self_design_recording import (
    DesignPlanRecord,
    DesignRecordAuthority,
    DesignRecordingEffect,
    DesignRecordingRequest,
    DesignRecordResult,
    DesignRecordStatus,
)
from af.framework.types import PROMPT_MAX_LENGTH
from af.framework.version_control import (
    CoordinationObservation,
    GitObjectFormat,
    InitializationStatus,
    RemoteObservation,
    RepositoryObservation,
    VersionControlInitialization,
    VersionControlManifest,
)
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository
from af.interfaces.mcp import create_mcp_server

EXPECTED_TOOLS = {
    "af_request_human_decision",
    "af_get_human_decision",
    "af_respond_human_decision",
    "af_revoke_human_decision",
    "af_start_workflow",
    "af_get_workflow",
    "af_resume_workflow",
    "af_cancel_workflow",
    "af_hello",
    "af_ask",
    "af_count_characters",
    "af_list_tasks",
    "af_get_task",
    "af_create_task",
    "af_update_task",
    "af_delete_task",
    "af_index_search_resources",
    "af_search",
    "af_route_agent",
}

_SELF_DESIGN_CAPTURED_AT = datetime(2026, 7, 24, 19, 0, tzinfo=UTC)

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


def _self_design_snapshot() -> DesignSnapshot:
    return DesignSnapshot(
        source_id="test.evidence",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha="a" * 40,
        captured_at=_SELF_DESIGN_CAPTURED_AT,
        candidates=(
            DesignCandidate(
                candidate_id="issue:124",
                revision="sha256:" + "b" * 64,
                kind=DesignUnitKind.CHILD_TASK,
                priority=0,
                blocking_impact=10,
                risk=1,
                created_at=_SELF_DESIGN_CAPTURED_AT,
                objective_id="objective:self-design",
                acceptance_clause_ids=("acceptance:read-only-command",),
                dependency_ids=(),
                active_overlap=False,
                acceptance_complete=True,
                append_only_safe=True,
                authority_granted=True,
                evidence_complete=True,
                complete=False,
            ),
        ),
    )


class _SelfDesignEvidenceProvider:
    def capture_snapshot(self) -> DesignSnapshot:
        return _self_design_snapshot()


class SelfDesignProvider(ServiceProvider):
    provider_id = "test.self-design"

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            SelfDesignOperation,
            lambda _: SelfDesignOperation(
                _SelfDesignEvidenceProvider(),
                DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
                lambda: _SELF_DESIGN_CAPTURED_AT,
            ),
        )


def _coordination(tmp_path) -> VersionControlInitialization:  # type: ignore[no-untyped-def]
    common_dir = tmp_path / ".git"
    af_dir = common_dir / "af"
    af_dir.mkdir(parents=True)
    manifest = VersionControlManifest(
        repository=RepositoryObservation(
            root=str(tmp_path),
            git_common_dir=str(common_dir),
            branch="master",
            head_sha="a" * 40,
            object_format=GitObjectFormat.SHA1,
        ),
        upstream_remote=RemoteObservation(
            name="origin",
            fetch_url="https://example.invalid/AgentFramework.git",
            push_url="https://example.invalid/AgentFramework.git",
        ),
        coordination=CoordinationObservation(
            remote_name="af-local",
            repository_path=str(af_dir / "remotes" / "coordination.git"),
            worktree_root=str(af_dir / "worktrees"),
        ),
    )
    manifest_path = af_dir / "version-control.json"
    manifest_path.write_bytes(manifest.file_bytes())
    return VersionControlInitialization(
        status=InitializationStatus.UNCHANGED,
        manifest=manifest,
        manifest_path=str(manifest_path),
    )


def _self_design_execution_result(tmp_path) -> SelfDesignExecutionResult:  # type: ignore[no-untyped-def]
    prepared = SelfDesignOperation(
        _SelfDesignEvidenceProvider(),
        DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
        lambda: _SELF_DESIGN_CAPTURED_AT,
    ).prepare()
    coordination = _coordination(tmp_path)
    authority = DesignRecordAuthority(
        authority_id="test:record-124",
        repository=prepared.snapshot.repository,
        base_ref=prepared.snapshot.base_ref,
        head_sha=prepared.snapshot.head_sha,
        snapshot_digest=prepared.snapshot.digest,
        plan_digest=prepared.plan.digest,
        version_control_manifest_digest=coordination.manifest.digest(),
        issued_at=_SELF_DESIGN_CAPTURED_AT,
        expires_at=_SELF_DESIGN_CAPTURED_AT + timedelta(minutes=5),
        allowed_effects=(DesignRecordingEffect.LOCAL_RECORD,),
    )
    record = DesignPlanRecord.from_request(
        DesignRecordingRequest(
            prepared=prepared,
            authority=authority,
            coordination=coordination,
            observed_at=_SELF_DESIGN_CAPTURED_AT,
        )
    )
    record_path = (
        tmp_path
        / ".git"
        / "af"
        / "self-design"
        / "records"
        / f"{prepared.plan.digest.removeprefix('sha256:')}.json"
    )
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(record.canonical_bytes())
    return SelfDesignExecutionResult(
        plan=prepared.plan,
        record=DesignRecordResult(
            status=DesignRecordStatus.RECORDED,
            record=record,
            record_path=str(record_path),
        ),
        publication=DesignPublicationReceipt(
            status=DesignPublicationStatus.PUBLISHED,
            effect=DesignPublicationEffect.CREATE_ISSUE,
            repository=prepared.snapshot.repository,
            issue_number=146,
            target_revision="sha256:" + "c" * 64,
            draft_digest="sha256:" + "d" * 64,
        ),
    )


class _RecordingSelfDesignExecutor:
    def __init__(self, result: SelfDesignExecutionResult) -> None:
        self.result = result
        self.preview_calls = 0
        self.execute_calls = 0

    def preview(self):  # type: ignore[no-untyped-def]
        self.preview_calls += 1
        return self.result.plan

    def execute(self) -> SelfDesignExecutionResult:
        self.execute_calls += 1
        return self.result


class SelfDesignExecutionProvider(ServiceProvider):
    provider_id = "test.self-design.execute"

    def __init__(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        self._tmp_path = tmp_path

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            SelfDesignExecutionController,
            lambda _: SelfDesignExecutionController(
                _RecordingSelfDesignExecutor(_self_design_execution_result(self._tmp_path))
            ),
        )


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
            assert tools["af_index_search_resources"].annotations is not None
            assert tools["af_index_search_resources"].annotations.readOnlyHint is False
            assert tools["af_search"].annotations is not None
            assert tools["af_search"].annotations.readOnlyHint is True
            assert tools["af_search"].annotations.idempotentHint is True
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

            indexed = await session.call_tool("af_index_search_resources", {})
            assert indexed.isError is False
            assert indexed.structuredContent == {
                "indexed": 5,
                "sources": [
                    {"source": "tasks", "indexed": 1},
                    {"source": "traces", "indexed": 4},
                ],
            }

            searched = await session.call_tool("af_search", {"text": "pineapple"})
            assert searched.isError is False
            assert searched.structuredContent is not None
            assert searched.structuredContent["total"] == 2
            assert {item["source_id"] for item in searched.structuredContent["items"]} == {
                "af_tasks",
                "af_run_traces",
            }

            deleted = await session.call_tool("af_delete_task", {"task_id": task_id})
            assert deleted.structuredContent == {"id": task_id, "deleted": True}

            resources = await session.list_resources()
            assert [str(resource.uri) for resource in resources.resources] == ["af://manifest"]

            manifest_result = await session.read_resource("af://manifest")
            manifest = json.loads(manifest_result.contents[0].text)
            assert manifest["version"].endswith("a0")
            assert manifest["routes"] == [
                "catalogue-execute",
                "character-count",
                "current-time",
                "hello",
                "science-calculate",
            ]
            assert manifest["route_identities"] == [
                {
                    "procedure_id": "catalogue-execute",
                    "version": "1.0.0",
                    "digest": manifest["route_identities"][0]["digest"],
                },
                {
                    "procedure_id": "character-count",
                    "version": "1.0.0",
                    "digest": manifest["route_identities"][1]["digest"],
                },
                {
                    "procedure_id": "current-time",
                    "version": "1.1.0",
                    "digest": manifest["route_identities"][2]["digest"],
                },
                {
                    "procedure_id": "hello",
                    "version": "1.0.0",
                    "digest": manifest["route_identities"][3]["digest"],
                },
                {
                    "procedure_id": "science-calculate",
                    "version": "1.0.0",
                    "digest": manifest["route_identities"][4]["digest"],
                },
            ]
            assert all(
                item["digest"].startswith("sha256:") for item in manifest["route_identities"]
            )
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


def test_mcp_exposes_configured_self_design_tool() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(SelfDesignProvider(),),
    )

    async def scenario() -> None:
        server = create_mcp_server(application)
        async with create_connected_server_and_client_session(server) as session:
            catalogue = await session.list_tools()
            tools = {tool.name: tool for tool in catalogue.tools}
            assert "af_self_design" in tools
            assert tools["af_self_design"].annotations is not None
            assert tools["af_self_design"].annotations.readOnlyHint is True
            assert tools["af_self_design"].annotations.idempotentHint is True

            planned = await session.call_tool("af_self_design", {})

            assert planned.isError is False
            assert planned.structuredContent == {
                "acceptance_clause_ids": ["acceptance:read-only-command"],
                "allowed_writes": [],
                "decision": {
                    "assessments": [{"candidate_id": "issue:124", "reason": "eligible"}],
                    "policy_digest": planned.structuredContent["decision"]["policy_digest"],
                    "reason": "selected",
                    "selected_candidate_id": "issue:124",
                    "snapshot_digest": planned.structuredContent["decision"]["snapshot_digest"],
                },
                "objective_id": "objective:self-design",
                "route": "propose",
                "selected_candidate_id": "issue:124",
                "stop_conditions": [
                    "ambiguity",
                    "authority_missing",
                    "cancelled",
                    "dependency_cycle",
                    "evidence_missing",
                    "overlap_detected",
                    "snapshot_stale",
                    "unsafe",
                ],
                "unit_kind": "child_task",
            }

    anyio.run(scenario)
    application.shutdown()


def test_mcp_exposes_configured_self_design_execute_tool(tmp_path) -> None:  # type: ignore[no-untyped-def]
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(SelfDesignExecutionProvider(tmp_path),),
    )

    async def scenario() -> None:
        server = create_mcp_server(application)
        async with create_connected_server_and_client_session(server) as session:
            catalogue = await session.list_tools()
            tools = {tool.name: tool for tool in catalogue.tools}
            assert "af_self_design_execute" in tools
            assert tools["af_self_design_execute"].annotations is not None
            assert tools["af_self_design_execute"].annotations.readOnlyHint is False
            assert tools["af_self_design_execute"].annotations.idempotentHint is False
            assert tools["af_self_design_execute"].annotations.openWorldHint is True

            preview = await session.call_tool("af_self_design_execute", {})

            assert preview.isError is False
            assert preview.structuredContent == {
                "acceptance_clause_ids": ["acceptance:read-only-command"],
                "allowed_writes": [],
                "decision": {
                    "assessments": [{"candidate_id": "issue:124", "reason": "eligible"}],
                    "policy_digest": preview.structuredContent["decision"]["policy_digest"],
                    "reason": "selected",
                    "selected_candidate_id": "issue:124",
                    "snapshot_digest": preview.structuredContent["decision"]["snapshot_digest"],
                },
                "objective_id": "objective:self-design",
                "route": "propose",
                "selected_candidate_id": "issue:124",
                "stop_conditions": [
                    "ambiguity",
                    "authority_missing",
                    "cancelled",
                    "dependency_cycle",
                    "evidence_missing",
                    "overlap_detected",
                    "snapshot_stale",
                    "unsafe",
                ],
                "unit_kind": "child_task",
            }

            executed = await session.call_tool("af_self_design_execute", {"execute": True})

            assert executed.isError is False
            assert executed.structuredContent is not None
            assert executed.structuredContent["plan"]["selected_candidate_id"] == "issue:124"
            assert executed.structuredContent["record"]["status"] == "recorded"
            assert "path" not in executed.structuredContent["record"]["record"]
            assert executed.structuredContent["publication"]["status"] == "published"
            assert executed.structuredContent["publication"]["issue_number"] == 146

    anyio.run(scenario)
    application.shutdown()


def test_mcp_self_design_binding_failures_are_not_silently_hidden() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    original_resolve = application.container.resolve

    def resolve(token: object) -> object:
        if token is SelfDesignOperation:
            raise PlatformError(
                PlatformReason.CONFIGURATION_INVALID,
                stage="mcp.self-design.evidence-provider",
            )
        return original_resolve(token)

    application.container.resolve = resolve  # type: ignore[assignment]

    with pytest.raises(PlatformError, match="configuration is invalid"):
        create_mcp_server(application)

    application.shutdown()


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
                ("af_search", {"text": "   "}, "prompt must not be blank"),
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
            host="127.0.0.1",
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
            host="127.0.0.1",
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


def test_mcp_server_requires_an_explicit_host_owned_application() -> None:
    with pytest.raises(TypeError):
        create_mcp_server()  # type: ignore[call-arg]
    injected = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    with TestClient(create_mcp_server(injected).streamable_http_app()):
        pass
    assert injected.lifecycle.state is ApplicationState.SERVING
    injected.shutdown()


def test_mcp_construction_failure_closes_internally_created_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    def fail_transport(_: object) -> None:
        raise RuntimeError("private assembly failure")

    monkeypatch.setattr(mcp_module, "_transport_security_for", fail_transport)
    with pytest.raises(RuntimeError, match="private assembly failure"):
        create_mcp_server(application)
    assert application.lifecycle.state is ApplicationState.SERVING
    application.shutdown()


def test_mcp_main_closes_application_when_server_construction_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    monkeypatch.setattr(mcp_module, "create_application", lambda: application)

    def fail_server(_: object) -> None:
        raise RuntimeError("private server failure")

    monkeypatch.setattr(mcp_module, "create_mcp_server", fail_server)
    with pytest.raises(RuntimeError, match="private server failure"):
        mcp_module.main()
    assert application.lifecycle.state is ApplicationState.SHUTDOWN


def test_mcp_main_sync_host_bridges_owned_async_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Provider(ServiceProvider):
        provider_id = "test.mcp-async-shutdown"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    class Server:
        def run(self, *, transport: object) -> None:
            del transport

    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(Provider(),),
    )
    monkeypatch.setattr(mcp_module, "create_application", lambda: application)
    monkeypatch.setattr(mcp_module, "create_mcp_server", lambda _: Server())

    mcp_module.main()

    assert application.lifecycle.state is ApplicationState.SHUTDOWN
    assert events == ["shutdown"]
