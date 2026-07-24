"""Installed AF console composition, including governed self-operation commands."""

from __future__ import annotations

from collections.abc import Callable

import typer

from af.application.version_control import VersionControlController
from af.bootstrap import AFApplication
from af.framework.errors import AFError, PlatformError, PlatformReason
from af.framework.self_design_operation import SelfDesignOperation
from af.interfaces.cli import create_cli as create_base_cli


def create_cli(
    application: AFApplication | None = None,
    *,
    application_factory: Callable[[], AFApplication] | None = None,
    version_control: VersionControlController | None = None,
    self_design_operation: SelfDesignOperation | None = None,
) -> typer.Typer:
    """Compose the installed CLI without giving self-design any write authority."""
    cli = create_base_cli(
        application,
        application_factory=application_factory,
        version_control=version_control,
    )

    @cli.command("self-design")
    def plan_self_design() -> None:
        """Emit one canonical read-only design plan from configured evidence."""
        try:
            if self_design_operation is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-design.evidence-provider",
                )
            typer.echo(self_design_operation.plan().canonical_json())
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    return cli


app = create_cli()
