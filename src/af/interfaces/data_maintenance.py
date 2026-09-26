"""Machine-readable CLI adapter for the injected data maintenance controller."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from af.application.data_maintenance import DataMaintenanceController
from af.framework.errors import AFError


def register_data_commands(
    cli: typer.Typer, controller: Callable[[typer.Context], DataMaintenanceController]
) -> None:
    group = typer.Typer(no_args_is_help=True, help="Initialize, back up and retain private data.")

    def emit(operation: Callable[[], dict[str, object]]) -> None:
        try:
            typer.echo(json.dumps(operation(), sort_keys=True))
        except (AFError, ValueError, OSError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    @group.command("init")
    def initialize(context: typer.Context) -> None:
        """Apply and verify the versioned reference schema."""
        emit(lambda: controller(context).initialize())

    @group.command("backup")
    def backup(context: typer.Context, destination: Path) -> None:
        """Create a private authenticated SQLite snapshot and manifest."""
        emit(lambda: controller(context).backup(destination))

    @group.command("restore")
    def restore(context: typer.Context, backup: Path, destination: Path) -> None:
        """Verify a backup and restore to a new destination."""
        emit(lambda: controller(context).restore(backup, destination))

    @group.command("prune")
    def prune(
        context: typer.Context,
        before: Annotated[str, typer.Option()],
        execute: Annotated[
            bool, typer.Option(help="Apply the previewed retention policy.")
        ] = False,
    ) -> None:
        """Preview expired core records; deletion requires --execute."""
        emit(
            lambda: controller(context).prune(
                before=datetime.fromisoformat(before), execute=execute
            )
        )

    cli.add_typer(group, name="data")
