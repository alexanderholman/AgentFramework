"""MCP adapter for local agents and remotely hosted ChatGPT integrations."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from af import __version__
from af.bootstrap import AFApplication, create_application
from af.domain.tasks import Task
from af.framework.errors import AFError
from af.framework.routing import ProcedureRegistry
from af.framework.types import ExecutionOutcome


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


def create_mcp_server(application: AFApplication | None = None) -> FastMCP[None]:
    resolved = application or create_application()
    controller = resolved.tasks
    registry = resolved.container.resolve(ProcedureRegistry)
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
    )

    @server.tool(
        name="af_hello",
        description="Run AF's tested static Hello World route.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_hello() -> OutcomeModel:
        return OutcomeModel.from_outcome(controller.execute("hello"))

    @server.tool(
        name="af_ask",
        description="Classify and execute a supported prompt, recording its AF trace.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_ask(prompt: str) -> OutcomeModel:
        try:
            return OutcomeModel.from_outcome(controller.execute(prompt))
        except AFError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        name="af_count_characters",
        description="Deterministically count one character in text through AF.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_count_characters(text: str, character: str) -> OutcomeModel:
        try:
            return OutcomeModel.from_outcome(controller.count_characters(text, character))
        except AFError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        name="af_list_tasks",
        description="List persisted AF Tasks.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
        structured_output=True,
    )
    def af_list_tasks() -> TaskListModel:
        items = [TaskModel.from_task(task) for task in controller.list()]
        return TaskListModel(items=items, total=len(items))

    @server.tool(
        name="af_get_task",
        description="Read one persisted AF Task by its exact ID.",
        annotations=_annotations(read_only=True, destructive=False, idempotent=True),
        structured_output=True,
    )
    def af_get_task(task_id: str) -> TaskModel:
        try:
            return TaskModel.from_task(controller.get(task_id))
        except AFError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        name="af_create_task",
        description="Execute a supported prompt and persist its verified AF Task.",
        annotations=_annotations(read_only=False, destructive=False, idempotent=False),
        structured_output=True,
    )
    def af_create_task(prompt: str) -> TaskModel:
        try:
            return TaskModel.from_task(controller.create(prompt))
        except AFError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        name="af_update_task",
        description="Replace a Task prompt and re-execute it through AF.",
        annotations=_annotations(read_only=False, destructive=True, idempotent=False),
        structured_output=True,
    )
    def af_update_task(task_id: str, prompt: str) -> TaskModel:
        try:
            return TaskModel.from_task(controller.update(task_id, prompt))
        except AFError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        name="af_delete_task",
        description="Delete one persisted AF Task by exact ID.",
        annotations=_annotations(read_only=False, destructive=True, idempotent=False),
        structured_output=True,
    )
    def af_delete_task(task_id: str) -> DeleteResult:
        try:
            controller.delete(task_id)
        except AFError as error:
            raise ToolError(str(error)) from error
        return DeleteResult(id=task_id, deleted=True)

    @server.resource(
        "af://manifest",
        name="AF manifest",
        description="Versioned AF lifecycle, route, and tier vocabulary.",
        mime_type="application/json",
    )
    def manifest() -> str:
        routes = sorted(descriptor.procedure_id for descriptor in registry.catalogue())
        tiers = sorted({descriptor.tier.name.lower() for descriptor in registry.catalogue()})
        return json.dumps(
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
                "routes": routes,
                "tiers": tiers,
            }
        )

    return server


def main() -> None:
    application = create_application()
    transport: Literal["stdio", "streamable-http"]
    if application.settings.mcp_transport == "streamable-http":
        transport = "streamable-http"
    else:
        transport = "stdio"
    create_mcp_server(application).run(transport=transport)


if __name__ == "__main__":
    main()
