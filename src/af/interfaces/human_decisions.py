"""Private CLI handoffs and JSON API for the same durable decision service."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from af.application.human_decisions import HumanDecisionService
from af.framework.context import current_context
from af.framework.errors import AFError
from af.interfaces.json_input import read_json
from af.interfaces.private_output import write_private_json


class HumanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=10_000, strict=True)
    expires_in: int = Field(default=300, ge=1, le=3600, strict=True)


class HumanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    response_secret: str = Field(min_length=1, max_length=128, strict=True)
    choice_id: str = Field(min_length=1, max_length=64, strict=True)


def register_human_decision_commands(
    cli: typer.Typer, factory: Callable[[typer.Context], HumanDecisionService]
) -> None:
    group = typer.Typer(no_args_is_help=True, help="Pause and resume bounded human decisions.")

    def emit(operation: Callable[[], dict[str, object]]) -> None:
        try:
            typer.echo(json.dumps(operation(), sort_keys=True))
        except (AFError, ValueError, OSError):
            typer.echo('{"kind":"Denied","reason":"decision_unavailable"}', err=True)
            raise typer.Exit(2) from None

    @group.command("request")
    def request(
        context: typer.Context,
        prompt: str,
        output: Annotated[Path, typer.Option()],
        expires_in: Annotated[int, typer.Option(min=1, max=3600)] = 300,
    ) -> None:
        def issue() -> dict[str, object]:
            service = factory(context)

            def compensate(result: dict[str, object]) -> None:
                if "decision_id" in result:
                    service.revoke(str(result["decision_id"]), current_context())

            result = write_private_json(
                output,
                lambda: service.submit(prompt, current_context(), expires_in=expires_in),
                compensate,
            )
            return {"kind": result["kind"], "decision_file": str(output)}

        emit(issue)

    @group.command("respond")
    def respond(
        context: typer.Context,
        decision: Annotated[Path, typer.Option()],
        choice: Annotated[str, typer.Option()],
    ) -> None:
        def resume() -> dict[str, object]:
            payload = read_json(decision, max_bytes=131_072)
            if not isinstance(payload, dict):
                raise ValueError("invalid decision")
            response = HumanResponse.model_validate(
                {
                    "decision_id": payload.get("decision_id"),
                    "response_secret": payload.get("response_secret"),
                    "choice_id": choice,
                }
            )
            return factory(context).resume(
                response.decision_id,
                response.response_secret,
                response.choice_id,
                current_context(),
            )

        emit(resume)

    @group.command("inspect")
    def inspect(context: typer.Context, decision_id: str) -> None:
        emit(lambda: factory(context).inspect(decision_id, current_context()))

    @group.command("revoke")
    def revoke(context: typer.Context, decision_id: str) -> None:
        emit(lambda: factory(context).revoke(decision_id, current_context()))

    cli.add_typer(group, name="resolve")


def create_human_decision_router(factory: Callable[[], HumanDecisionService]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/decisions", tags=["human-decisions"])

    def call(operation: Callable[[], dict[str, object]]) -> dict[str, object]:
        try:
            return operation()
        except (AFError, ValueError):
            raise HTTPException(409, detail="decision_unavailable") from None

    @router.post("")
    def submit(request: HumanRequest) -> dict[str, object]:
        return call(
            lambda: factory().submit(
                request.prompt, current_context(), expires_in=request.expires_in
            )
        )

    @router.post("/respond")
    def resume(response: HumanResponse) -> dict[str, object]:
        return call(
            lambda: factory().resume(
                response.decision_id,
                response.response_secret,
                response.choice_id,
                current_context(),
            )
        )

    @router.get("/{decision_id}")
    def inspect(decision_id: str) -> dict[str, object]:
        return call(lambda: factory().inspect(decision_id, current_context()))

    @router.post("/{decision_id}/revoke")
    def revoke(decision_id: str) -> dict[str, object]:
        return call(lambda: factory().revoke(decision_id, current_context()))

    return router
