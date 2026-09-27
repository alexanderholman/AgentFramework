"""Pure capability catalogue discovery and preview; never executes a candidate."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

import typer

from af.application.capability_preview import (
    CapabilityPreviewService,
    CapabilitySnapshot,
    import_capability_catalogue,
)
from af.framework.capabilities import RequestRequirements
from af.interfaces.json_input import read_json


def register_capability_commands(
    cli: typer.Typer, snapshot: Callable[[typer.Context], CapabilitySnapshot]
) -> None:
    @cli.command("capabilities")
    def capabilities(
        context: typer.Context,
        catalogue: Annotated[Path | None, typer.Option()] = None,
        request: Annotated[Path | None, typer.Option()] = None,
        as_json: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """List an evidence snapshot or preview a typed request without executing it."""
        del as_json
        try:
            selected = snapshot(context)
            if catalogue is not None:
                payload = read_json(catalogue, max_bytes=16 * 1_048_576)
                if not isinstance(payload, dict):
                    raise ValueError("catalogue must be an object")
                selected = import_capability_catalogue(
                    cast(dict[str, object], payload),
                    source=catalogue.name,
                    observed_at=datetime.now(UTC),
                )
            if request is None:
                result = selected.to_dict()
            else:
                requirements = RequestRequirements.from_dict(read_json(request))
                result = CapabilityPreviewService(selected).preview(requirements).to_dict()
            typer.echo(json.dumps(result, sort_keys=True))
        except (ValueError, TypeError, OSError):
            typer.echo('{"error":"invalid_capability_request"}', err=True)
            raise typer.Exit(2) from None
