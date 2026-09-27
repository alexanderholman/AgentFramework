"""CLI and HTTP workflow transports delegate to the same controller."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated

import typer
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from af.application.workflow_control import WorkflowController
from af.framework.context import current_context
from af.framework.workflows import NodeState, WorkflowError, WorkflowRun


def workflow_to_dict(run: WorkflowRun) -> dict[str, object]:
    outputs: list[dict[str, object]] = []
    for node in run.nodes:
        if node.output is not None:
            try:
                value = json.loads(node.output)
            except (ValueError, UnicodeError):
                value = {"bytes": len(node.output), "digest": node.output_digest}
            outputs.append({"node": node.node_id, "value": value})
    return {
        "schema_version": "1.0.0",
        "run_id": run.run_id,
        "revision": run.revision,
        "complete": all(node.state is NodeState.COMPLETED for node in run.nodes),
        "cancelled": run.cancelled,
        "nodes": [
            {"id": node.node_id, "state": node.state.value, "verified": node.verified}
            for node in run.nodes
        ],
        "outputs": outputs,
    }


def register_workflow_commands(
    cli: typer.Typer, controller: Callable[[typer.Context], WorkflowController]
) -> None:
    group = typer.Typer(no_args_is_help=True, help="Run and resume installed durable workflows.")

    def emit(operation: Callable[[], WorkflowRun]) -> None:
        try:
            typer.echo(json.dumps(workflow_to_dict(operation()), sort_keys=True))
        except (WorkflowError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    @group.command("start")
    def start(
        context: typer.Context,
        data: str,
        name: str = "text.count-total",
        max_nodes: Annotated[int, typer.Option(min=1, max=128)] = 128,
    ) -> None:
        emit(
            lambda: controller(context).start(
                name, data.encode(), current_context(), max_nodes=max_nodes
            )
        )

    @group.command("resume")
    def resume(
        context: typer.Context,
        run_id: str,
        max_nodes: Annotated[int, typer.Option(min=1, max=128)] = 128,
    ) -> None:
        emit(lambda: controller(context).resume(run_id, current_context(), max_nodes=max_nodes))

    @group.command("status")
    def inspect(context: typer.Context, run_id: str) -> None:
        emit(lambda: controller(context).inspect(run_id, current_context()))

    @group.command("cancel")
    def cancel(context: typer.Context, run_id: str) -> None:
        emit(lambda: controller(context).cancel(run_id, current_context()))

    cli.add_typer(group, name="workflows")


class WorkflowStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="text.count-total", max_length=128)
    data: str = Field(max_length=65_536)
    max_nodes: int = Field(default=128, ge=1, le=128)


def create_workflow_router(controller: WorkflowController) -> APIRouter:
    router = APIRouter(prefix="/api/v1/workflows", tags=["workflows"])

    def run(operation: Callable[[], WorkflowRun]) -> dict[str, object]:
        try:
            return workflow_to_dict(operation())
        except (WorkflowError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from None

    @router.post("")
    def start(request: WorkflowStart) -> dict[str, object]:
        return run(
            lambda: controller.start(
                request.name, request.data.encode(), current_context(), max_nodes=request.max_nodes
            )
        )

    @router.get("/{run_id}")
    def inspect(run_id: str) -> dict[str, object]:
        return run(lambda: controller.inspect(run_id, current_context()))

    @router.post("/{run_id}/resume")
    def resume(run_id: str) -> dict[str, object]:
        return run(lambda: controller.resume(run_id, current_context()))

    @router.post("/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, object]:
        return run(lambda: controller.cancel(run_id, current_context()))

    return router
