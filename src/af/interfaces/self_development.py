"""Thin public planning and explicitly configured local development commands."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from af.application.self_development import SelfDevelopmentController
from af.framework.context import current_context
from af.framework.errors import AFError
from af.framework.self_development_execution import DevelopmentPlanningInput


def register_self_development_commands(
    cli: typer.Typer, controller: Callable[[typer.Context], SelfDevelopmentController]
) -> None:
    @cli.command("self-develop")
    def self_develop(
        context: typer.Context,
        snapshot: Annotated[
            Path | None, typer.Option(help="Read-only development snapshot JSON.")
        ] = None,
        execute: Annotated[bool, typer.Option(help="Execute one exact configured plan.")] = False,
        plan_digest: Annotated[
            str | None, typer.Option(help="Digest returned by configured planning.")
        ] = None,
    ) -> None:
        try:
            service = controller(context)
            invocation = current_context()
            if execute:
                if snapshot is not None or plan_digest is None:
                    raise ValueError(
                        "execution requires --plan-digest and a configured source; no --snapshot"
                    )
                result = service.execute(plan_digest, invocation)
                typer.echo(result.model_dump_json())
                return
            if plan_digest is not None:
                raise ValueError("--plan-digest requires --execute")
            inputs = None
            if snapshot is not None:
                with snapshot.open("rb") as stream:
                    data = stream.read(1_048_577)
                if len(data) > 1_048_576:
                    raise ValueError("development snapshot exceeds the 1 MiB input bound")
                inputs = DevelopmentPlanningInput.model_validate_json(data)
            typer.echo(service.plan(inputs, context=invocation).model_dump_json())
        except (AFError, ValueError, OSError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(2) from None
