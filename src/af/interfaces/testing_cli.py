"""Developer-only pytest adapter; independent of runtime application composition."""

from __future__ import annotations

import importlib.util
import subprocess
import sys

import typer


def register_test_command(cli: typer.Typer) -> None:
    @cli.command(
        "test",
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )
    def run_tests(context: typer.Context) -> None:
        """Run trusted project tests with this environment's pytest; forward its arguments."""
        if importlib.util.find_spec("pytest") is None:
            typer.echo("pytest is not installed; install the project's development extra", err=True)
            raise typer.Exit(2)
        try:
            result = subprocess.run([sys.executable, "-m", "pytest", *context.args], check=False)
        except KeyboardInterrupt:
            raise typer.Exit(130) from None
        except OSError:
            typer.echo("could not start pytest", err=True)
            raise typer.Exit(2) from None
        code = result.returncode
        raise typer.Exit(code if code >= 0 else 128 - code)
