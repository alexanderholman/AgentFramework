"""Provider-discovered terminal applications and explicit headless/plain CLI modes."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from af.application.locales import LocaleSelection, Localizer
from af.application.task_tui import TaskTuiApplication
from af.application.tui import TuiKernel
from af.framework.constitution import PresentationPreferences
from af.framework.context import ExecutionContext, maybe_current_context
from af.framework.errors import ServiceNotBound
from af.framework.tui import (
    TUI_APPLICATIONS_TAG,
    KeyEvent,
    MouseEvent,
    ResizeEvent,
    TextEvent,
    TuiApplicationDescriptor,
    TuiContribution,
    TuiEvent,
    TuiRegistry,
)
from af.infrastructure.tui import ScriptedTuiDriver, TerminalTuiDriver
from af.interfaces.json_input import read_json

if TYPE_CHECKING:
    from af.bootstrap import AFApplication


def create_tui_kernel(application: AFApplication) -> TuiKernel:
    registry = TuiRegistry()
    registry.register(
        TuiApplicationDescriptor("tasks", "AF Tasks"),
        lambda: TaskTuiApplication(application.tasks, application.container.resolve(Localizer)),
    )
    try:
        contributions = application.container.resolve_tagged(TUI_APPLICATIONS_TAG)
    except ServiceNotBound:
        contributions = ()
    for contribution in contributions:
        if not isinstance(contribution, TuiContribution):
            raise ValueError("invalid TUI provider contribution")
        registry.register(contribution.descriptor, contribution.factory)
    return TuiKernel(application.container, registry)


def parse_script(payload: object) -> tuple[TuiEvent, ...]:
    if not isinstance(payload, dict) or set(payload) != {"events"}:
        raise ValueError("invalid TUI script")
    values = payload["events"]
    if not isinstance(values, list) or len(values) > 10_000:
        raise ValueError("invalid TUI script events")
    events: list[TuiEvent] = []
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("invalid TUI script event")
        kind = item.get("type")
        if kind == "text" and set(item) == {"type", "text"}:
            events.append(TextEvent(item["text"]))
        elif kind == "key" and set(item) == {"type", "key"}:
            events.append(KeyEvent(item["key"]))
        elif kind == "resize" and set(item) == {"type", "columns", "rows"}:
            events.append(ResizeEvent(item["columns"], item["rows"]))
        elif kind == "mouse" and set(item) == {"type", "widget_id"}:
            events.append(MouseEvent(item["widget_id"]))
        else:
            raise ValueError("invalid TUI script event")
    return tuple(events)


def register_tui_commands(cli: typer.Typer, factory: Callable[[typer.Context], TuiKernel]) -> None:
    group = typer.Typer(no_args_is_help=True, help="Run provider-installed terminal applications.")

    @group.command("list")
    def list_apps(context: typer.Context) -> None:
        typer.echo(
            json.dumps(
                [asdict(item) for item in factory(context).registry.catalogue()], sort_keys=True
            )
        )

    @group.command("run")
    def run(
        context: typer.Context,
        application_id: Annotated[str, typer.Argument()] = "tasks",
        plain: Annotated[bool, typer.Option("--plain")] = False,
        script: Annotated[Path | None, typer.Option()] = None,
        language: Annotated[str, typer.Option()] = "en",
        territory: Annotated[str | None, typer.Option()] = None,
    ) -> None:
        try:
            if plain and script is not None:
                raise ValueError("choose one TUI input mode")
            selected = LocaleSelection.parse(language=language, territory=territory)
            presentation = PresentationPreferences(plain_text=plain)
            driver = (
                ScriptedTuiDriver(parse_script(read_json(script)))
                if script
                else TerminalTuiDriver(plain=presentation.plain_text)
            )
            inherited = maybe_current_context()
            invocation = ExecutionContext(
                principal=inherited.principal if inherited else None,
                tenant_id=inherited.tenant_id if inherited else None,
                locale=selected.language + ("-" + selected.territory if selected.territory else ""),
            )
            result = factory(context).run(
                application_id, driver, context=invocation, plain_fallback=plain
            )
        except (ValueError, TypeError, OSError):
            typer.echo('{"error":"invalid_tui_request"}', err=True)
            raise typer.Exit(2) from None
        if script is not None:
            typer.echo(json.dumps(asdict(result), sort_keys=True))
        elif result.exit_code:
            typer.echo(json.dumps({"error": result.reason}), err=True)
        if result.exit_code:
            raise typer.Exit(result.exit_code)

    cli.add_typer(group, name="tui")
