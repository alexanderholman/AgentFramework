"""Local operator token commands; secrets are written only to private new files."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import typer

from af.application.authentication import AuthenticationService
from af.framework.authentication import IssuedToken
from af.framework.context import Principal
from af.framework.errors import AFError
from af.interfaces.json_input import read_json


def register_authentication_commands(
    cli: typer.Typer,
    service: Callable[[typer.Context], AuthenticationService],
    tenant: Callable[[typer.Context], str],
) -> None:
    group = typer.Typer(no_args_is_help=True, help="Manage local operator-issued scoped tokens.")

    def write_token(
        context: typer.Context, output: Path, operation: Callable[[], IssuedToken]
    ) -> None:
        issued = None
        owned: os.stat_result | None = None
        try:
            # Claim the private destination before changing token state.
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                owned = os.fstat(stream.fileno())
                issued = operation()
                json.dump(asdict(issued), stream)
                stream.flush()
                os.fsync(stream.fileno())
            typer.echo(
                json.dumps(
                    {
                        "token_id": issued.token_id,
                        "expires_at": issued.expires_at,
                        "scopes": issued.scopes,
                        "credential_file": str(output),
                    },
                    sort_keys=True,
                )
            )
        except (AFError, ValueError, OSError):
            if issued is not None:
                with suppress(AFError, ValueError, OSError):
                    service(context).revoke(issued.token_id, tenant_id=tenant(context))
            if owned is not None:
                with suppress(OSError):
                    current = output.stat(follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                        output.unlink()
            typer.echo('{"error":"token_operation_failed"}', err=True)
            raise typer.Exit(2) from None

    @group.command("issue")
    def issue(
        context: typer.Context,
        subject: Annotated[str, typer.Option()],
        scope: Annotated[list[str], typer.Option()],
        output: Annotated[Path, typer.Option()],
        ttl_seconds: Annotated[float, typer.Option(min=1, max=2_592_000)] = 3600,
    ) -> None:
        """OS-authorized host administration: issue exactly the specified scopes."""
        write_token(
            context,
            output,
            lambda: service(context).issue(
                Principal(subject, "user", scopes=tuple(scope)),
                tenant_id=tenant(context),
                ttl_seconds=ttl_seconds,
            ),
        )

    @group.command("rotate")
    def rotate(
        context: typer.Context,
        credential: Annotated[Path, typer.Option()],
        output: Annotated[Path, typer.Option()],
    ) -> None:
        def rotate_existing() -> IssuedToken:
            data = read_json(credential, max_bytes=4096)
            if not isinstance(data, dict) or not isinstance(data.get("secret"), str):
                raise ValueError("invalid credential file")
            return service(context).rotate(data["secret"], tenant_id=tenant(context))

        write_token(context, output, rotate_existing)

    @group.command("revoke")
    def revoke(context: typer.Context, token_id: str) -> None:
        try:
            result = service(context).revoke(token_id, tenant_id=tenant(context))
            typer.echo(json.dumps({"revoked": result}))
        except (AFError, ValueError, OSError):
            typer.echo('{"error":"token_operation_failed"}', err=True)
            raise typer.Exit(2) from None

    cli.add_typer(group, name="auth")
