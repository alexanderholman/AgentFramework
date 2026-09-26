"""Thin CLI adapters for remaining real read-only lifecycle stages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from af.application.self_stages import SelfStageController
from af.framework.errors import AFError
from af.framework.self_operation import SelfOperationStage
from af.framework.self_stages import StageEvidenceSnapshot

_STAGE_COMMANDS = (
    ("self-prioritise", SelfOperationStage.PRIORITISATION),
    ("self-specification", SelfOperationStage.SPECIFICATION),
    ("self-contain", SelfOperationStage.CONTAINMENT),
    ("self-assurance", SelfOperationStage.ASSURANCE),
    ("self-qualification", SelfOperationStage.QUALIFICATION),
    ("self-optimise", SelfOperationStage.OPTIMISATION),
)


def register_lifecycle_stage_commands(
    cli: typer.Typer, controller: Callable[[typer.Context], SelfStageController]
) -> None:
    def register(name: str, stage: SelfOperationStage) -> None:
        @cli.command(name)
        def plan(
            context: typer.Context,
            snapshot: Annotated[
                Path | None, typer.Option(help="Exact governed stage JSON.")
            ] = None,
        ) -> None:
            """Evaluate one bounded read-only stage from exact governed evidence."""
            try:
                evidence = None
                if snapshot is not None:
                    if snapshot.stat().st_size > 1_048_576:
                        raise ValueError("stage snapshot exceeds the 1 MiB input bound")
                    evidence = StageEvidenceSnapshot.model_validate_json(snapshot.read_bytes())
                typer.echo(controller(context).evaluate(stage, evidence).canonical_json())
            except (AFError, ValueError, OSError) as error:
                typer.echo(str(error), err=True)
                raise typer.Exit(code=2) from None

    for name, stage in _STAGE_COMMANDS:
        register(name, stage)
