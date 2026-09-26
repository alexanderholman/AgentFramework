"""JSON-only decision CLI; all consent and provider behavior lives in the application."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Never

import typer

from af.application.decisions import (
    DecisionController,
    decision_request_schema,
    parse_request,
    preview_payload,
)
from af.framework.decision_consent import DecisionConsent
from af.framework.decisions import DecisionStatus
from af.framework.errors import AFError
from af.interfaces.json_input import read_json as _read_json


def _consent(path: Path) -> DecisionConsent:
    data = _read_json(path)
    if (
        type(data) is not dict
        or set(data) != {"token"}
        or type(data["token"]) is not str
        or not 1 <= len(data["token"]) <= 256
    ):
        raise ValueError("invalid consent file")
    return DecisionConsent(data["token"])


def _fail() -> Never:
    typer.echo(json.dumps({"error": "invalid_or_unavailable_decision_request"}), err=True)
    raise typer.Exit(2) from None


def register_decision_commands(
    cli: typer.Typer, factory: Callable[[typer.Context], DecisionController]
) -> None:
    group = typer.Typer(help="Preview and explicitly authorize one typed remote decision.")
    cli.add_typer(group, name="decisions")

    @group.command("schema")
    def schema() -> None:
        typer.echo(json.dumps(decision_request_schema(), sort_keys=True))

    @group.command("preview")
    def preview(
        context: typer.Context,
        request: Annotated[Path, typer.Option()],
        expires_in: Annotated[float, typer.Option()] = 300.0,
    ) -> None:
        try:
            result = factory(context).preview(
                parse_request(_read_json(request)), expires_in=expires_in
            )
            typer.echo(json.dumps(preview_payload(result), sort_keys=True))
        except (AFError, ValueError, TypeError, OSError):
            _fail()

    @group.command("approve")
    def approve(
        context: typer.Context,
        request: Annotated[Path, typer.Option()],
        preview: Annotated[Path, typer.Option()],
        max_cost_usd: Annotated[str, typer.Option()],
        output: Annotated[Path, typer.Option()],
    ) -> None:
        """Authorize the exact shown disclosure and write a private one-use token file."""
        controller: DecisionController | None = None
        grant: DecisionConsent | None = None
        try:
            controller = factory(context)
            grant = controller.approve(
                parse_request(_read_json(request)),
                _read_json(preview),
                max_cost_usd=Decimal(max_cost_usd),
            )
            # Refuse replacement or symlink traversal, and never print the token.
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                json.dump({"token": grant.token}, stream)
            typer.echo(
                json.dumps({"status": "approved", "consent_file": str(output)}, sort_keys=True)
            )
        except (AFError, ValueError, TypeError, OSError, InvalidOperation):
            if controller is not None and grant is not None:
                controller.revoke(grant)
            _fail()

    @group.command("evaluate")
    def evaluate(
        context: typer.Context,
        request: Annotated[Path, typer.Option()],
        consent: Annotated[Path, typer.Option()],
    ) -> None:
        try:
            answer = factory(context).evaluate(
                parse_request(_read_json(request)), _consent(consent)
            )
            typer.echo(json.dumps(asdict(answer), sort_keys=True))
            if answer.status is not DecisionStatus.ANSWERED:
                raise typer.Exit(2)
        except (AFError, ValueError, TypeError, OSError):
            _fail()

    @group.command("receipt")
    def receipt(context: typer.Context, consent: Annotated[Path, typer.Option()]) -> None:
        try:
            result = factory(context).receipt(_consent(consent))
            typer.echo(json.dumps(None if result is None else asdict(result), sort_keys=True))
            if result is None:
                raise typer.Exit(1)
        except (AFError, ValueError, TypeError, OSError):
            _fail()

    @group.command("revoke")
    def revoke(context: typer.Context, consent: Annotated[Path, typer.Option()]) -> None:
        try:
            factory(context).revoke(_consent(consent))
            typer.echo(json.dumps({"status": "revocation_requested"}))
        except (AFError, ValueError, TypeError, OSError):
            _fail()
