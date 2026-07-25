"""Installed AF console composition, including governed self-operation commands."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

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

    # --- Database setup sub-commands ---
    db_cli = typer.Typer(
        name="db",
        help="Database service setup for local development.",
    )

    @db_cli.command("setup")
    def setup_mariadb_command(
        user: str = typer.Option(
            "af_test", "--user", "-u", help="Database user for AF integration tests.",
        ),
        password: str = typer.Option(
            "af_test_pass", "--password", "-p", help="Password for the test user.",
        ),
        database: str = typer.Option(
            "af_test", "--database", "-d", help="Database name for AF integration tests.",
        ),
        host: str = typer.Option(
            "localhost", "--host", "-h", help="Host to connect to.",
        ),
        port: int = typer.Option(
            3306, "--port", help="Port to connect on.",
        ),
        project_root: Path = typer.Option(  # noqa: B008
            None, "--project-root", "-r",
            help="Project root directory (for default .env location).",
        ),
    ) -> None:
        """Set up MariaDB: install, start service, create database and user, write .env."""
        from af.infrastructure.db_setup import setup_mariadb

        try:
            if project_root is None:
                project_root = Path.cwd()
            url = setup_mariadb(
                user=user,
                password=password,
                database=database,
                host=host,
                port=port,
                project_root=project_root,
            )
            typer.echo(f"\nSetup complete. Connection URL: {url}")
        except Exception as error:
            typer.echo(f"MariaDB setup failed: {error}", err=True)
            raise typer.Exit(code=1) from None

    cli.add_typer(db_cli)

    return cli


app = create_cli()
