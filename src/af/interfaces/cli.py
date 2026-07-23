"""Typer console kernel: AF's Artisan-like operator interface."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from af.application.ideons import InterpretRequest
from af.bootstrap import AFApplication, create_application
from af.domain.ideons.records import ContextGraph
from af.framework.errors import AFError
from af.interfaces.presenters import agent_route_to_dict, delete_to_dict, task_to_dict


def create_cli(application: AFApplication | None = None) -> typer.Typer:
    cli = typer.Typer(name="af", no_args_is_help=True, help="AF Agent Framework")
    task_commands = typer.Typer(name="tasks", no_args_is_help=True, help="Persisted Task CRUD")
    ideon_commands = typer.Typer(
        name="ideon",
        no_args_is_help=True,
        help="Deterministic Prime Language / Ideon reference",
    )
    cached_application = application

    def current() -> AFApplication:
        nonlocal cached_application
        if cached_application is None:
            cached_application = create_application()
        return cached_application

    def fail(error: Exception) -> None:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2)

    @cli.command()
    def hello() -> None:
        """Run the static Hello World route."""
        try:
            typer.echo(current().tasks.execute("hello").output)
        except AFError as error:
            fail(error)

    @cli.command()
    def ask(prompt: Annotated[str, typer.Argument(help="Natural-language work request")]) -> None:
        """Classify and execute a request without persisting a Task."""
        try:
            typer.echo(current().tasks.execute(prompt).output)
        except AFError as error:
            fail(error)

    @cli.command("route-agent")
    def route_agent(
        prompt: Annotated[str, typer.Argument(help="Work request to route to an agent role")],
    ) -> None:
        """Return AF's deterministic agent-role routing decision as JSON."""
        try:
            typer.echo(json.dumps(agent_route_to_dict(current().agent_routes.route(prompt))))
        except AFError as error:
            fail(error)

    @ideon_commands.command("interpret")
    def interpret_ideon(
        surface: Annotated[str, typer.Argument(help="Text to interpret")],
        actor: Annotated[str | None, typer.Option(help="Inherited actor context")] = None,
        domain: Annotated[str | None, typer.Option(help="Domain context, such as river")] = None,
    ) -> None:
        """Emit the actual canonical structured interpretation record."""
        values: dict[str, str] = {}
        if actor is not None:
            values["actor"] = actor
        if domain is not None:
            values["domain"] = domain
        try:
            context = ContextGraph.from_dict(
                values,
                identity_fields=tuple(sorted(values)),
            )
            record = current().ideons.interpret(
                InterpretRequest(
                    surface=surface,
                    source_id="cli:input",
                    rights="user-provided",
                    context=context,
                )
            )
            typer.echo(record.canonical_bytes().decode("utf-8"))
        except (AFError, TypeError, ValueError) as error:
            fail(error)

    @cli.command()
    def serve(
        host: Annotated[str | None, typer.Option()] = None,
        port: Annotated[int | None, typer.Option()] = None,
        allowed_host: Annotated[list[str] | None, typer.Option("--allowed-host")] = None,
    ) -> None:
        """Serve HTML, REST, and GraphQL interfaces."""
        import uvicorn

        from af.interfaces.http import create_http_app

        resolved = current()
        uvicorn.run(
            create_http_app(
                resolved,
                bind_host=host or resolved.settings.host,
                allowed_hosts=tuple(allowed_host or ()) or None,
            ),
            host=host or resolved.settings.host,
            port=port or resolved.settings.port,
        )

    @task_commands.command("create")
    def create_task(prompt: str) -> None:
        try:
            typer.echo(json.dumps(task_to_dict(current().tasks.create(prompt))))
        except AFError as error:
            fail(error)

    @task_commands.command("list")
    def list_tasks() -> None:
        typer.echo(json.dumps([task_to_dict(task) for task in current().tasks.list()]))

    @task_commands.command("show")
    def show_task(task_id: str) -> None:
        try:
            typer.echo(json.dumps(task_to_dict(current().tasks.get(task_id))))
        except AFError as error:
            fail(error)

    @task_commands.command("update")
    def update_task(task_id: str, prompt: str) -> None:
        try:
            typer.echo(json.dumps(task_to_dict(current().tasks.update(task_id, prompt))))
        except AFError as error:
            fail(error)

    @task_commands.command("delete")
    def delete_task(task_id: str) -> None:
        try:
            current().tasks.delete(task_id)
            typer.echo(json.dumps(delete_to_dict(task_id)))
        except AFError as error:
            fail(error)

    cli.add_typer(task_commands, name="tasks")
    cli.add_typer(ideon_commands, name="ideon")
    return cli


app = create_cli()
