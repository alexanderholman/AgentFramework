"""Local OAuth action commands with bounded JSON and private one-time outputs."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from af.application.oauth_controller import OAuthController
from af.framework.errors import AFError
from af.framework.oauth_grants import OAuthGrantError
from af.interfaces.json_input import read_json
from af.interfaces.private_output import write_private_json


def register_oauth_commands(
    cli: typer.Typer, factory: Callable[[typer.Context], OAuthController]
) -> None:
    group = typer.Typer(
        no_args_is_help=True, help="Headless OAuth actions for authorized local operators."
    )

    def install(action: str) -> None:
        @group.command(action)
        def execute(
            context: typer.Context,
            request: Annotated[Path, typer.Option()],
            output: Annotated[Path, typer.Option()],
        ) -> None:
            try:
                payload = read_json(request, max_bytes=65_536)
                controller = factory(context)
                write_private_json(
                    output,
                    lambda: controller.run(action, payload),
                    lambda result: controller.cancel_output(action, payload, result),
                )
                typer.echo(json.dumps({"completed": True, "result_file": str(output)}))
            except (AFError, ValueError, OSError) as error:
                code = error.code if isinstance(error, OAuthGrantError) else "oauth_action_failed"
                typer.echo(json.dumps({"error": code}), err=True)
                raise typer.Exit(2) from None

    for action in OAuthController.actions:
        install(action)
    cli.add_typer(group, name="oauth")
