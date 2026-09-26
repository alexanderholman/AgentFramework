"""Machine-readable adapters over installed AF contracts."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Annotated

import typer
from pydantic import TypeAdapter

from af.application.introspection import IntrospectionService
from af.framework.capabilities import CapabilityDescriptor, RequestRequirements
from af.framework.decisions import DecisionAnswer, DecisionRequest
from af.framework.evolution import EvolutionSnapshot
from af.framework.jobs import JobEvent
from af.framework.managers import DriverManifest
from af.framework.self_improvement import ImprovementSnapshot
from af.framework.types import (
    ProcedureDescriptor,
    SelectionProposal,
    SelectorMetadata,
    VerificationResult,
    WorkRequest,
)
from af.framework.workflows import WorkflowRun
from af.interfaces.human_interpreter import HumanInterpretRequest
from af.interfaces.presenters import procedure_descriptor_to_dict

_SCHEMAS: dict[str, type[object]] = {
    "execution-request": WorkRequest,
    "human-interpretation-request": HumanInterpretRequest,
    "decision-request": DecisionRequest,
    "decision-result": DecisionAnswer,
    "workflow-checkpoint": WorkflowRun,
    "capability-request": RequestRequirements,
    "evolution-snapshot": EvolutionSnapshot,
    "improvement-snapshot": ImprovementSnapshot,
    "procedure-proposal": SelectionProposal,
    "procedure-descriptor": ProcedureDescriptor,
    "verification-result": VerificationResult,
    "job-event": JobEvent,
    "capability-descriptor": CapabilityDescriptor,
    "provider-descriptor": SelectorMetadata,
    "driver-descriptor": DriverManifest,
}


def contract_schema(name: str) -> dict[str, object]:
    if name not in _SCHEMAS:
        raise ValueError("unknown schema name")
    return TypeAdapter(_SCHEMAS[name]).json_schema()


def register_introspection(
    cli: typer.Typer,
    service: Callable[[typer.Context], IntrospectionService],
    doctor_report: Callable[[], dict[str, object]],
) -> None:
    dev = typer.Typer(help="Discover the canonical AF engineering workflow.")

    @dev.command("describe")
    def describe(
        context: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False
    ) -> None:
        del as_json
        typer.echo(json.dumps(service(context).describe(), sort_keys=True))

    @cli.command("procedures")
    def procedures(
        context: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False
    ) -> None:
        del as_json
        typer.echo(
            json.dumps(
                [procedure_descriptor_to_dict(item) for item in service(context).procedures()]
            )
        )

    @cli.command("providers")
    def providers(
        context: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False
    ) -> None:
        del as_json
        typer.echo(
            json.dumps(
                [{"id": item, "state": "registered"} for item in service(context).providers()]
            )
        )

    @cli.command("schemas")
    def schemas(name: Annotated[str | None, typer.Argument()] = None) -> None:
        try:
            value = sorted(_SCHEMAS) if name is None else contract_schema(name)
            typer.echo(json.dumps(value, sort_keys=True))
        except ValueError:
            typer.echo("unknown schema name", err=True)
            raise typer.Exit(code=2) from None

    @cli.command("drivers")
    def drivers(
        context: typer.Context, as_json: Annotated[bool, typer.Option("--json")] = False
    ) -> None:
        del as_json
        descriptors = TypeAdapter(tuple[DriverManifest, ...]).dump_python(
            service(context).drivers(), mode="json"
        )
        typer.echo(
            json.dumps(
                {"registered": descriptors, "service_qualification": "not_performed"},
                sort_keys=True,
            )
        )

    @cli.command("doctor")
    def doctor(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
        del as_json
        try:
            report = doctor_report()
        except (ValueError, TypeError):
            typer.echo(json.dumps({"ready": False, "checks": {"configuration": "invalid"}}))
            raise typer.Exit(code=2) from None
        typer.echo(json.dumps(report, sort_keys=True))
        if not report["ready"]:
            raise typer.Exit(code=1)

    cli.add_typer(dev, name="dev")
