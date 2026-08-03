"""Installed AF console composition, including governed self-operation commands."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from af.application.database_setup import (
    DatabaseSetupController,
    DatabaseSetupMode,
    DatabaseSetupRequest,
)
from af.application.self_design_execution import SelfDesignExecutionController
from af.application.version_control import VersionControlController
from af.bootstrap import AFApplication
from af.framework.errors import AFError, PlatformError, PlatformReason, ServiceNotBound
from af.framework.self_alignment_operation import SelfAlignmentOperation
from af.framework.self_analysis_operation import SelfAnalysisOperation
from af.framework.self_design_operation import SelfDesignOperation
from af.interfaces.cli import create_cli as create_base_cli
from af.interfaces.cli import current_application_from_context
from af.interfaces.cli_interactions import CliInteraction


def create_cli(
    application: AFApplication | None = None,
    *,
    application_factory: Callable[[], AFApplication] | None = None,
    interaction_factory: Callable[[], CliInteraction] | None = None,
    database_setup: DatabaseSetupController | None = None,
    version_control: VersionControlController | None = None,
    self_alignment_operation: SelfAlignmentOperation | None = None,
    self_analysis_operation: SelfAnalysisOperation | None = None,
    self_design_operation: SelfDesignOperation | None = None,
    self_design_execution: SelfDesignExecutionController | None = None,
) -> typer.Typer:
    """Compose the installed CLI without granting self-operation write authority."""
    cli = create_base_cli(
        application,
        application_factory=application_factory,
        interaction_factory=interaction_factory,
        version_control=version_control,
    )

    @cli.command("self-align")
    def plan_self_align() -> None:
        """Emit one canonical read-only alignment plan from configured evidence."""
        try:
            if self_alignment_operation is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.self-align.evidence-provider",
                )
            typer.echo(self_alignment_operation.plan().canonical_json())
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

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

    db_cli = typer.Typer(
        name="db",
        no_args_is_help=True,
        help="Safe local database setup for AF integration testing.",
    )

    @db_cli.command("setup")
    def setup_database(
        context: typer.Context,
        mode: Annotated[DatabaseSetupMode, typer.Option("--mode")] = DatabaseSetupMode.LOCAL_DEBIAN,
        user: Annotated[
            str,
            typer.Option("--user", help="Database user to create or update."),
        ] = "af_test",
        password: Annotated[
            str,
            typer.Option("--password", help="Password for the configured database user."),
        ] = "af_test_pass",
        database: Annotated[
            str,
            typer.Option("--database", help="Database name to create or reuse."),
        ] = "af_test",
        host: Annotated[
            str,
            typer.Option(
                "--host",
                help="Connection host. local-debian mode only supports localhost or 127.0.0.1.",
            ),
        ] = "127.0.0.1",
        port: Annotated[int, typer.Option("--port", help="Connection port.")] = 3306,
        project_root: Annotated[
            Path | None,
            typer.Option("--project-root", help="Project root whose .env file should be updated."),
        ] = None,
        execute: Annotated[
            bool,
            typer.Option(
                "--execute",
                help="Apply provisioning, verification, and env updates after previewing the plan.",
            ),
        ] = False,
    ) -> None:
        """Preview or apply safe local MariaDB setup through the application boundary."""
        try:
            controller = database_setup
            if controller is None:
                try:
                    controller = current_application_from_context(context).container.resolve(
                        DatabaseSetupController
                    )
                except ServiceNotBound:
                    controller = None
            if controller is None:
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.db-setup.controller",
                )
            result = controller.setup(
                DatabaseSetupRequest(
                    mode=mode,
                    user=user,
                    password=password,
                    database=database,
                    host=host,
                    port=port,
                    project_root=Path.cwd() if project_root is None else project_root,
                    execute=execute,
                )
            )
            typer.echo(
                json.dumps(
                    result.to_dict(),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (AFError, TypeError, ValueError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    cli.add_typer(db_cli, name="db")

    return cli


app = create_cli()
