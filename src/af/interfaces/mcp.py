"""MCP adapter for local agents and remotely hosted ChatGPT integrations."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import Literal, cast

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from af import __version__
from af.application.search import SearchController
from af.application.self_design_execution import SelfDesignExecutionController
from af.application.tasks import TaskController
from af.bootstrap import AFApplication, create_application
from af.domain.agent_routing import (
    AgentRole,
    AgentRouteReason,
    AgentRouteResult,
    AgentRouteStatus,
)
from af.domain.tasks import Task
from af.framework.errors import AFError, InvalidProviderConfiguration, ServiceNotBound
from af.framework.routing import ProcedureRegistry
from af.framework.self_design_operation import SelfDesignOperation
from af.framework.types import ExecutionOutcome
from af.interfaces.http_trust import (
    ParsedHost,
    format_host,
    format_origin,
    parse_host_value,
    parse_origin_value,
)

MCP_INTERNAL_ERROR_MESSAGE = "internal MCP adapter error (MCP_INTERNAL_ERROR)"


class OutcomeModel(BaseModel):
    prompt: str
    output: str
    intent: str
    route: str
    tier: str
    status: str
    verified: bool
    reused: bool
    trace_id: str

    @classmethod
    def from_outcome(cls, outcome: ExecutionOutcome) -> OutcomeModel:
        return cls.model_validate(outcome, from_attributes=True)


class TaskModel(BaseModel):
    id: str
    prompt: str
    output: str
    intent: str
    route: str
    tier: str
    status: str
    verified: bool
    reused: bool
    trace_id: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_task(cls, task: Task) -> TaskModel:
        return cls.model_validate(task, from_attributes=True)


class TaskListModel(BaseModel):
    items: list[TaskModel]
    total: int


class DeleteResult(BaseModel):
    id: str
    deleted: bool


class SearchHitModel(BaseModel):
    resource_id: str
    source_id: str
    authoritative_locator: str
    tenant_id: str
    projection_version: int
    acl_evidence: list[str]
    provenance: list[str]
    score: float
    rank: int
    highlights: list[str]


class SearchResultModel(BaseModel):
    items: list[SearchHitModel]
    total: int


class SearchIndexSourceModel(BaseModel):
    source: str
    indexed: int


class SearchIndexResultModel(BaseModel):
    indexed: int
    sources: list[SearchIndexSourceModel]


class AgentRouteModel(BaseModel):
    prompt: str
    status: AgentRouteStatus
    agent: AgentRole | None
    reason: AgentRouteReason
    candidates: list[AgentRole]

    @classmethod
    def from_route(cls, route: AgentRouteResult) -> AgentRouteModel:
        return cls.model_validate(route, from_attributes=True)


def _annotations(
    *,
    read_only: bool,
    destructive: bool,
    idempotent: bool,
) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def _invoke_mcp_tool[T](callback: Callable[[], T]) -> T:
    af_error: AFError | None = None
    unexpected_failure = False
    try:
        return callback()
    except AFError as error:
        af_error = error
    except Exception:
        unexpected_failure = True
    if af_error is not None:
        raise ToolError(str(af_error))
    if unexpected_failure:
        raise ToolError(MCP_INTERNAL_ERROR_MESSAGE)
    raise AssertionError("unreachable")


def _invoke_mcp_resource[T](callback: Callable[[], T]) -> T:
    af_error: AFError | None = None
    unexpected_failure = False
    try:
        return callback()
    except AFError as error:
        af_error = error
    except Exception:
        unexpected_failure = True
    if af_error is not None:
        raise ValueError(str(af_error))
    if unexpected_failure:
        raise ValueError(MCP_INTERNAL_ERROR_MESSAGE)
    raise AssertionError("unreachable")


def create_mcp_server(application: AFApplication) -> FastMCP[None]:
    """Build an MCP adapter; the invoking host retains application ownership."""
    return _assemble_mcp_server(application)


def _assemble_mcp_server(resolved: AFApplication) -> FastMCP[None]:
    controller = resolved.tasks
    search = resolved.search
    agent_routes = resolved.agent_routes
    registry = resolved.container.resolve(ProcedureRegistry)
    transport_security = _transport_security_for(resolved)
    self_design_operation = _resolve_self_design_operation(resolved)
    self_design_execution = _resolve_self_design_execution(resolved)

    server: FastMCP[None] = FastMCP(
        name="AF Agent Framework",
        instructions=(
            "AF classifies, routes, executes, verifies, traces, tests and reuses "
            "supported work. Persist requests only with af_create_task. Never invent "
            "task IDs or present unverified output as final."
        ),
        host=resolved.settings.host,
        port=resolved.settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=transport_security,
    )

    @server.tool(
        name="af_hello",
        description="Run AF's tested static Hello World route.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_hello() -> OutcomeModel:
        return _invoke_mcp_tool(lambda: OutcomeModel.from_outcome(controller.execute("hello")))

    @server.tool(
        name="af_ask",
        description="Classify and execute a supported prompt, recording its AF trace.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_ask(prompt: str) -> OutcomeModel:
        return _invoke_mcp_tool(lambda: OutcomeModel.from_outcome(controller.execute(prompt)))

    @server.tool(
        name="af_count_characters",
        description="Deterministically count one character in text through AF.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_count_characters(text: str, character: str) -> OutcomeModel:
        return _invoke_mcp_tool(
            lambda: OutcomeModel.from_outcome(controller.count_characters(text, character))
        )

    @server.tool(
        name="af_route_agent",
        description=(
            "Deterministically select one installed agent role or return an ambiguous handoff."
        ),
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
        structured_output=True,
    )
    def af_route_agent(prompt: str) -> AgentRouteModel:
        return _invoke_mcp_tool(lambda: AgentRouteModel.from_route(agent_routes.route(prompt)))

    if self_design_operation is not None:

        @server.tool(
            name="af_self_design",
            description="Emit one canonical read-only self-design plan from configured evidence.",
            annotations=_annotations(read_only=True, destructive=False, idempotent=True),
            structured_output=True,
        )
        def af_self_design() -> dict[str, object]:
            return _invoke_mcp_tool(
                lambda: cast(
                    dict[str, object],
                    json.loads(self_design_operation.plan().canonical_json()),
                )
            )

    if self_design_execution is not None:

        @server.tool(
            name="af_self_design_execute",
            description=(
                "Preview or explicitly execute one configured governed self-design cycle."
            ),
            annotations=_annotations(read_only=False, destructive=False, idempotent=False),
            structured_output=True,
        )
        def af_self_design_execute(execute: bool = False) -> dict[str, object]:
            return _invoke_mcp_tool(
                lambda: cast(
                    dict[str, object],
                    self_design_execution.execute().to_dict()
                    if execute
                    else json.loads(self_design_execution.preview().canonical_json()),
                )
            )

    @server.tool(
        name="af_list_tasks",
        description="List persisted AF Tasks.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
        structured_output=True,
    )
    def af_list_tasks() -> TaskListModel:
        return _invoke_mcp_tool(lambda: _list_tasks(controller))

    @server.tool(
        name="af_get_task",
        description="Read one persisted AF Task by its exact ID.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
        structured_output=True,
    )
    def af_get_task(task_id: str) -> TaskModel:
        return _invoke_mcp_tool(lambda: TaskModel.from_task(controller.get(task_id)))

    @server.tool(
        name="af_create_task",
        description="Execute a supported prompt and persist its verified AF Task.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_create_task(prompt: str) -> TaskModel:
        return _invoke_mcp_tool(lambda: TaskModel.from_task(controller.create(prompt)))

    @server.tool(
        name="af_update_task",
        description="Replace a Task prompt and re-execute it through AF.",
        annotations=_annotations(read_only=False, destructive=True, idempotent=False),
        structured_output=True,
    )
    def af_update_task(task_id: str, prompt: str) -> TaskModel:
        return _invoke_mcp_tool(lambda: TaskModel.from_task(controller.update(task_id, prompt)))

    @server.tool(
        name="af_delete_task",
        description="Delete one persisted AF Task by exact ID.",
        annotations=_annotations(read_only=False, destructive=True, idempotent=False),
        structured_output=True,
    )
    def af_delete_task(task_id: str) -> DeleteResult:
        return _invoke_mcp_tool(lambda: _delete_task(controller, task_id))

    @server.tool(
        name="af_index_search_resources",
        description="Index persisted AF task and trace history into the configured search driver.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_index_search_resources(limit: int = 100) -> SearchIndexResultModel:
        return _invoke_mcp_tool(lambda: _index_search(search, limit=limit))

    @server.tool(
        name="af_search",
        description="Query indexed AF history through the canonical tenant-safe search API.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
        structured_output=True,
    )
    def af_search(text: str, limit: int = 20) -> SearchResultModel:
        return _invoke_mcp_tool(lambda: _search(search, text=text, limit=limit))

    @server.resource(
        "af://manifest",
        name="AF manifest",
        description="Versioned AF lifecycle, route, and tier vocabulary.",
        mime_type="application/json",
    )
    def manifest() -> str:
        return _invoke_mcp_resource(
            lambda: json.dumps(
                {
                    "name": "AF Agent Framework",
                    "version": __version__,
                    "lifecycle": [
                        "classify",
                        "route",
                        "execute",
                        "verify",
                        "record",
                        "test",
                        "reuse",
                    ],
                    "routes": sorted(
                        descriptor.procedure_id for descriptor in registry.catalogue()
                    ),
                    "tiers": sorted(
                        {descriptor.tier.name.lower() for descriptor in registry.catalogue()}
                    ),
                    "agent_roles": sorted(role.value for role in AgentRole),
                }
            )
        )

    return server


def _list_tasks(controller: TaskController) -> TaskListModel:
    tasks = controller.list()
    items = [TaskModel.from_task(task) for task in tasks]
    return TaskListModel(items=items, total=len(items))


def _delete_task(controller: TaskController, task_id: str) -> DeleteResult:
    controller.delete(task_id)
    return DeleteResult(id=task_id, deleted=True)


def _resolve_self_design_operation(application: AFApplication) -> SelfDesignOperation | None:
    try:
        operation = application.container.resolve(SelfDesignOperation)
    except ServiceNotBound:
        return None
    return operation if isinstance(operation, SelfDesignOperation) else None


def _resolve_self_design_execution(
    application: AFApplication,
) -> SelfDesignExecutionController | None:
    try:
        controller = application.container.resolve(SelfDesignExecutionController)
    except ServiceNotBound:
        return None
    return controller if isinstance(controller, SelfDesignExecutionController) else None


def _index_search(search: SearchController, *, limit: int) -> SearchIndexResultModel:
    result = search.index(limit=limit)
    return SearchIndexResultModel.model_validate(
        {
            "indexed": result.indexed,
            "sources": [
                {"source": item.source, "indexed": item.indexed} for item in result.sources
            ],
        }
    )


def _search(search: SearchController, *, text: str, limit: int) -> SearchResultModel:
    result = search.search(text, limit=limit)
    return SearchResultModel.model_validate(
        {
            "items": [
                {
                    "resource_id": item.resource_id,
                    "source_id": item.source_id,
                    "authoritative_locator": item.authoritative_locator,
                    "tenant_id": item.tenant_id,
                    "projection_version": item.projection_version,
                    "acl_evidence": list(item.acl_evidence),
                    "provenance": list(item.provenance),
                    "score": item.score,
                    "rank": item.rank,
                    "highlights": list(item.highlights),
                }
                for item in result.items
            ],
            "total": result.total,
        }
    )


def _transport_security_for(
    application: AFApplication,
) -> TransportSecuritySettings | None:
    settings = application.settings
    if settings.mcp_transport != "streamable-http":
        return None
    if settings.allowed_hosts:
        _canonical_transport_hosts(settings.allowed_hosts, port=settings.port)
    if settings.allowed_origins:
        _canonical_transport_origins(settings.allowed_origins)
    if _is_loopback_host(settings.host):
        return None
    if not settings.allowed_hosts or not settings.allowed_origins:
        raise InvalidProviderConfiguration(
            "streamable HTTP MCP requires explicit allowed hosts and origins for non-loopback hosts"
        )
    allowed_hosts = _canonical_transport_hosts(settings.allowed_hosts, port=settings.port)
    allowed_origins = _canonical_transport_origins(settings.allowed_origins)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _is_loopback_host(host: str) -> bool:
    try:
        return parse_host_value(host, error_message="host is invalid").host in {
            "127.0.0.1",
            "::1",
            "localhost",
        }
    except InvalidProviderConfiguration:
        return False


def _canonical_transport_hosts(values: tuple[str, ...], *, port: int) -> list[str]:
    canonical: list[str] = []
    seen: set[str] = set()
    for value in values:
        parsed = parse_host_value(value, error_message="allowed host is invalid")
        for candidate in _host_transport_candidates(parsed, port=port):
            if candidate not in seen:
                seen.add(candidate)
                canonical.append(candidate)
    return canonical


def _host_transport_candidates(parsed: ParsedHost, *, port: int) -> tuple[str, ...]:
    if parsed.port is not None:
        return (format_host(parsed),)
    return (format_host(parsed), format_host(ParsedHost(parsed.host, port)))


def _canonical_transport_origins(values: tuple[str, ...]) -> list[str]:
    canonical: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = format_origin(
            parse_origin_value(value, error_message="allowed origin is invalid")
        )
        if candidate not in seen:
            seen.add(candidate)
            canonical.append(candidate)
    return canonical


def main() -> None:
    application = create_application()
    try:
        transport: Literal["stdio", "streamable-http"]
        if application.settings.mcp_transport == "streamable-http":
            transport = "streamable-http"
        else:
            transport = "stdio"
        create_mcp_server(application).run(transport=transport)
    except BaseException:
        with suppress(BaseException):
            application.shutdown()
        raise
    else:
        application.shutdown()


if __name__ == "__main__":
    main()
