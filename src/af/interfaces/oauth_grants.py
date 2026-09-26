"""Local OAuth action commands with bounded JSON and private one-time outputs."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from af.application.oauth_controller import OAuthController
from af.framework.errors import AFError
from af.framework.oauth_grants import OAuthGrantError
from af.interfaces.json_input import read_json


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
            result = None
            controller = None
            payload = None
            claimed = False
            try:
                payload = read_json(request, max_bytes=16_384)
                controller = factory(context)
                descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                claimed = True
                with os.fdopen(descriptor, "w") as stream:
                    result = controller.run(action, payload)
                    json.dump(result, stream, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                typer.echo(json.dumps({"completed": True, "result_file": str(output)}))
            except (AFError, ValueError, OSError) as error:
                if controller is not None and result is not None:
                    controller.cancel_output(action, payload, result)
                if claimed:
                    output.unlink(missing_ok=True)
                code = error.code if isinstance(error, OAuthGrantError) else "oauth_action_failed"
                typer.echo(json.dumps({"error": code}), err=True)
                raise typer.Exit(2) from None

    for action in OAuthController.actions:
        install(action)
    cli.add_typer(group, name="oauth")
