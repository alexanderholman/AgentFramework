"""Explicit configured/fixture CLI adapters for bounded self-operation."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated
from uuid import uuid4

import typer

from af.application.self_improvement import SelfImprovementService
from af.framework.context import maybe_current_context
from af.framework.errors import AFError
from af.framework.evolution import EvolutionPlan, EvolutionSnapshot, plan_evolution
from af.framework.self_improvement import ImprovementBudget


def register_self_improvement_commands(
    cli: typer.Typer,
    configured_service: Callable[[typer.Context], SelfImprovementService],
    fixture_service: Callable[[typer.Context], SelfImprovementService],
    evolution_fixture: Callable[[], EvolutionSnapshot],
) -> None:
    @cli.command("self-improve")
    def self_improve(
        context: typer.Context,
        operation_id: Annotated[str | None, typer.Option()] = None,
        fixture: Annotated[
            bool, typer.Option(help="Run the offline evidence-only fixture.")
        ] = False,
        maximum_stages: Annotated[int, typer.Option(min=1, max=100)] = 10,
        maximum_cost: Annotated[int, typer.Option(min=1, max=1_000_000)] = 100,
        timeout_seconds: Annotated[int, typer.Option(min=1, max=3600)] = 60,
    ) -> None:
        """Run configured admitted stage ports under explicit bounded operation limits."""
        try:
            service = fixture_service(context) if fixture else configured_service(context)
            invocation = maybe_current_context()
            deadline = datetime.now(UTC) + timedelta(seconds=timeout_seconds)
            if invocation is not None and invocation.deadline is not None:
                deadline = min(deadline, invocation.deadline)
            result = service.run(
                operation_id or f"operation-{uuid4().hex}",
                ImprovementBudget(
                    deadline=deadline,
                    maximum_stages=maximum_stages,
                    maximum_cost=maximum_cost,
                ),
                cancellation=None if invocation is None else invocation.cancellation,
            )
            payload = result.model_dump(mode="json")
            payload["fixture"] = fixture
            typer.echo(json.dumps(payload, sort_keys=True))
        except (AFError, ValueError, OSError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None

    @cli.command("evolve")
    def evolve(
        snapshot: Annotated[
            Path | None, typer.Option(help="Governed longitudinal snapshot JSON.")
        ] = None,
        fixture: Annotated[
            bool, typer.Option(help="Use explicit offline fixture evidence.")
        ] = False,
    ) -> None:
        """Produce one deterministic proposal or typed stop; no repository effects."""
        try:
            if (snapshot is None) == (not fixture):
                raise ValueError("choose exactly one of --snapshot or --fixture")
            if snapshot is not None:
                if snapshot.stat().st_size > 1_048_576:
                    raise ValueError("evolution snapshot exceeds the 1 MiB input bound")
                value = EvolutionSnapshot.model_validate_json(snapshot.read_bytes())
            else:
                value = evolution_fixture()
            result = plan_evolution(value)
            payload = json.loads(result.canonical_json())
            if isinstance(result, EvolutionPlan):
                payload["proposal_digest"] = result.digest
            payload["fixture"] = fixture
            typer.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        except (AFError, ValueError, OSError) as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(code=2) from None
