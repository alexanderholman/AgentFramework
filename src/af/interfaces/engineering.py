"""Explicit local engineering commands; route evidence is always derived inside AF."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from af.application.engineering_controller import EngineeringController
from af.framework.context import current_context
from af.framework.engineering import EngineeringRequest
from af.framework.errors import AFError


def register_engineering_commands(
    cli: typer.Typer, controller: Callable[[typer.Context], EngineeringController]
) -> None:
    @cli.command("engineer")
    def engineer(
        context: typer.Context,
        input_file: Annotated[
            Path, typer.Option("--input", help="JSON with a bounded texts array.")
        ],
    ) -> None:
        """Engineer and verify the confined local nonblank-line-count capability."""
        try:
            with input_file.open("rb") as stream:
                data = stream.read(262_145)
            if len(data) > 262_144:
                raise ValueError("engineering input exceeds the 256 KiB encoded bound")
            request = EngineeringRequest.model_validate_json(data)
            result = controller(context).complete(request, current_context())
            typer.echo(result.model_dump_json())
        except (AFError, ValueError, OSError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(2) from None

    group = typer.Typer(help="Inspect or invalidate the admitted local capability.")
    cli.add_typer(group, name="engineering")

    @group.command("status")
    def status(context: typer.Context) -> None:
        try:
            admission = controller(context).inspect()
            typer.echo(admission.model_dump_json() if admission is not None else "null")
        except (AFError, ValueError, OSError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(2) from None

    @group.command("invalidate")
    def invalidate(
        context: typer.Context,
        reason: Annotated[str, typer.Option(help="Evidence-backed rollback reason.")],
    ) -> None:
        try:
            controller(context).invalidate(reason, current_context())
            typer.echo('{"state":"invalidated"}')
        except (AFError, ValueError, OSError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(2) from None
