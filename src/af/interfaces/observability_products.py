"""Optional authorized JSON inspector, metric cards and installed admin actions."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated

import typer

from af.application.admin import AdminService
from af.application.authentication import AuthenticationService
from af.application.observability_products import InspectorService, MetricsService
from af.framework.context import Principal
from af.framework.errors import AFError
from af.framework.observability_products import InspectorKind, ObservabilityUnavailable
from af.interfaces.json_input import read_json


@dataclass(frozen=True, slots=True)
class ObservabilityProducts:
    inspector: InspectorService | None = None
    metrics: MetricsService | None = None
    admin: AdminService | None = None


def register_observability_commands(
    cli: typer.Typer,
    factory: Callable[[typer.Context], ObservabilityProducts],
    authentication: Callable[[typer.Context], AuthenticationService],
    tenant: Callable[[typer.Context], str],
) -> None:
    group = typer.Typer(
        no_args_is_help=True, help="Query optional observational products with scoped credentials."
    )

    def principal(context: typer.Context, credential: Path) -> Principal:
        payload = read_json(credential, max_bytes=4096)
        if not isinstance(payload, dict) or not isinstance(payload.get("secret"), str):
            raise ValueError("invalid operator credential")
        return authentication(context).authenticate(payload["secret"], tenant_id=tenant(context))

    def perform(operation: Callable[[], object]) -> None:
        try:
            typer.echo(json.dumps(operation(), sort_keys=True, allow_nan=False))
        except (AFError, ValueError, OSError):
            typer.echo('{"error":"observability_operation_failed"}', err=True)
            raise typer.Exit(2) from None

    @group.command("inspector")
    def inspector(
        context: typer.Context,
        credential: Annotated[Path, typer.Option()],
        kind: InspectorKind | None = None,
        name: str | None = None,
        tag: str | None = None,
        limit: int = 100,
    ) -> None:
        def read() -> object:
            actor = principal(context, credential)
            product = factory(context).inspector
            if product is None:
                raise ObservabilityUnavailable()
            return [
                asdict(item)
                for item in product.entries(actor, kind=kind, name=name, tag=tag, limit=limit)
            ]

        perform(read)

    @group.command("metrics")
    def metrics(context: typer.Context, credential: Annotated[Path, typer.Option()]) -> None:
        def read() -> object:
            actor = principal(context, credential)
            product = factory(context).metrics
            if product is None:
                raise ObservabilityUnavailable()
            return [asdict(item) for item in product.cards(actor)]

        perform(read)

    @group.command("purge")
    def purge(
        context: typer.Context,
        product: str,
        credential: Annotated[Path, typer.Option()],
        confirm: bool = False,
    ) -> None:
        def remove() -> object:
            actor = principal(context, credential)
            products = factory(context)
            selected = (
                products.inspector
                if product == "inspector"
                else products.metrics
                if product == "metrics"
                else None
            )
            if selected is None or not confirm:
                raise ObservabilityUnavailable()
            return {"removed": selected.purge(actor)}

        perform(remove)

    @group.command("admin-query")
    def admin_query(
        context: typer.Context,
        resource: str,
        credential: Annotated[Path, typer.Option()],
        limit: int = 100,
    ) -> None:
        def read() -> object:
            actor = principal(context, credential)
            product = factory(context).admin
            if product is None:
                raise ObservabilityUnavailable()
            return [asdict(item) for item in product.query(actor, resource, limit=limit)]

        perform(read)

    @group.command("admin-action")
    def admin_action(
        context: typer.Context,
        resource: str,
        action: str,
        credential: Annotated[Path, typer.Option()],
        request: Annotated[Path, typer.Option()],
        confirm: bool = False,
    ) -> None:
        def run() -> object:
            actor = principal(context, credential)
            product = factory(context).admin
            parameters = read_json(request, max_bytes=8192)
            if product is None or not isinstance(parameters, dict):
                raise ObservabilityUnavailable()
            return asdict(product.action(actor, resource, action, parameters, confirmed=confirm))

        perform(run)

    cli.add_typer(group, name="observe")
