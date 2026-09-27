"""Installed starter command; generated files belong to the application."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from af.infrastructure.application_scaffold import StarterTemplate, scaffold_application


def register_starter_commands(cli: typer.Typer) -> None:
    @cli.command("new")
    def new(
        root: Path, template: StarterTemplate = StarterTemplate.BASIC_WEB, package: str = "app"
    ) -> None:
        try:
            result = scaffold_application(root=root, package=package, template=template)
            typer.echo(json.dumps(result.to_dict(), sort_keys=True))
        except (ValueError, OSError):
            typer.echo(
                "starter generation failed; choose a valid package and new writable directory",
                err=True,
            )
            raise typer.Exit(2) from None
