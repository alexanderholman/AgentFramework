"""CLI and HTTP translation for the shared finite human-language boundary."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

import typer
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from af.application.human_interpreter import HumanInterpreter
from af.domain.algorithms.contracts import AlgorithmError, canonical, parse_json
from af.domain.human_interpreter import (
    MAX_SIGNAL_BYTES,
    InterpreterError,
    ObservedSignal,
    SourceKind,
)
from af.domain.ideons.records import ContextGraph


class HumanInterpretRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=MAX_SIGNAL_BYTES, strict=True)
    source_id: str = Field(default="request:user", min_length=1, max_length=200, strict=True)
    source_kind: SourceKind = SourceKind.USER
    rights: str = Field(default="user-provided", min_length=1, max_length=200, strict=True)
    received_at: str | None = Field(default=None, max_length=64, strict=True)
    context: dict[str, str] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def validate_observation(self) -> HumanInterpretRequest:
        self.to_signal()
        if any(len(key) > 64 or len(value) > 256 for key, value in self.context.items()):
            raise ValueError("context bound exceeded")
        ContextGraph.from_dict(self.context)
        return self

    def to_signal(self) -> ObservedSignal:
        return ObservedSignal(
            self.text.encode("utf-8"),
            self.source_id,
            self.source_kind,
            datetime.fromisoformat(self.received_at) if self.received_at else datetime.now(UTC),
            self.rights,
        )


def interpret_request(
    service: HumanInterpreter, request: HumanInterpretRequest
) -> dict[str, object]:
    return service.interpret(request.to_signal(), ContextGraph.from_dict(request.context)).to_dict()


def register_human_interpreter_commands(
    cli: typer.Typer, service: Callable[[typer.Context], HumanInterpreter]
) -> None:
    group = typer.Typer(
        no_args_is_help=True, help="Inspect source-bound meaning without executing it."
    )

    def emit(context: typer.Context, create: Callable[[], HumanInterpretRequest]) -> None:
        try:
            typer.echo(json.dumps(interpret_request(service(context), create()), sort_keys=True))
        except (InterpreterError, AlgorithmError, ValueError, UnicodeError):
            typer.echo("invalid human interpretation request", err=True)
            raise typer.Exit(code=2) from None

    @group.command("interpret")
    def interpret(
        context: typer.Context,
        text: str,
        source_id: str = "request:user",
        source_kind: SourceKind = SourceKind.USER,
        rights: str = "user-provided",
        received_at: Annotated[
            str | None, typer.Option(help="Aware ISO time; defaults to receipt time.")
        ] = None,
    ) -> None:
        emit(
            context,
            lambda: HumanInterpretRequest(
                text=text,
                source_id=source_id,
                source_kind=source_kind,
                rights=rights,
                received_at=received_at,
            ),
        )

    @group.command("interpret-json")
    def interpret_json(context: typer.Context, data: str) -> None:
        def create() -> HumanInterpretRequest:
            raw = parse_json(data.encode("utf-8"))
            return HumanInterpretRequest.model_validate_json(canonical(raw))

        emit(context, create)

    @group.command("templates")
    def templates(context: typer.Context) -> None:
        typer.echo(service(context).templates_snapshot().decode())

    cli.add_typer(group, name="human")


def create_human_interpreter_router(service: HumanInterpreter) -> APIRouter:
    router = APIRouter(prefix="/api/v1/human", tags=["human-interpretation"])

    @router.post("/interpret")
    def interpret(request: HumanInterpretRequest) -> dict[str, object]:
        try:
            return interpret_request(service, request)
        except (InterpreterError, ValueError):
            raise HTTPException(
                status_code=422, detail="invalid human interpretation request"
            ) from None

    return router
