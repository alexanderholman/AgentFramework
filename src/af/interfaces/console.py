"""Installed AF console composition, including governed self-operation commands."""

from __future__ import annotations

import json
from collections.abc import Callable

import typer

from af.application.self_design_execution import SelfDesignExecutionController
from af.application.version_control import VersionControlController
from af.bootstrap import AFApplication
from af.framework.errors import AFError, PlatformError, PlatformReason, ServiceNotBound
from af.framework.self_analysis_operation import SelfAnalysisOperation
from af.framework.self_design_operation import SelfDesignOperation
from af.interfaces.cli import create_cli as create_base_cli
from af.interfaces.cli import current_application_from_context


def create_cli(
    application: AFApplication | None = None,
    *,
    application_factory: Callable[[], AFApplication] | None = None,
    version_control: VersionControlController | None = None,
    self_analysis_operation: SelfAnalysisOperation | None = None,
    self_design_operation: SelfDesignOperation | None = None,
    self_design_execution: SelfDesignExecutionController | None = None,
) -> typer.Typer:
    """Compose the installed CLI without granting self-operation write authority."""
    cli = create_base_cli(
        application,
        application_factory=application_factory,
        version_control=version_control,
    )

    @cli.command("self-analysis")
    def plan_self_analysis() -> None:
        """Emit one canonical read-only analysis plan from configured evidence."""
        try:
            if self_analysis_operation is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-analysis.evidence-provider",
                )
            typer.echo(self_analysis_operation.plan().canonical_json())
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    @cli.command("self-design")
    def plan_self_design(context: typer.Context) -> None:
        """Emit one canonical read-only design plan from configured evidence."""
        try:
            operation = self_design_operation
            if operation is None:
                try:
                    operation = current_application_from_context(context).container.resolve(
                        SelfDesignOperation
                    )
                except ServiceNotBound:
                    operation = None
            if operation is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-design.evidence-provider",
                )
            typer.echo(operation.plan().canonical_json())
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    @cli.command("self-design-execute")
    def execute_self_design(
        context: typer.Context,
        execute: bool = typer.Option(
            False,
            "--execute",
            help="Apply one explicitly configured governed self-design cycle.",
        ),
    ) -> None:
        """Preview or apply one explicit governed self-design mutation cycle."""
        try:
            controller = self_design_execution
            if controller is None:
                try:
                    controller = current_application_from_context(context).container.resolve(
                        SelfDesignExecutionController
                    )
                except ServiceNotBound:
                    controller = None
            if controller is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-design.execution-controller",
                )
            if not execute:
                typer.echo(controller.preview().canonical_json())
                return
            typer.echo(
                json.dumps(
                    controller.execute().to_dict(),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    return cli


app = create_cli()
