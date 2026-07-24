"""Typer console kernel: AF's Artisan-like operator interface."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from af.application.ideons import InterpretRequest
from af.bootstrap import AFApplication, create_application
from af.domain.ideons.records import ContextGraph
from af.framework.errors import AFError
from af.framework.migrations import MigrationDirection, MigrationPlan, MigrationRunner
from af.interfaces.presenters import agent_route_to_dict, delete_to_dict, task_to_dict


def create_cli(application: AFApplication | None = None) -> typer.Typer:
    cli = typer.Typer(name="af", no_args_is_help=True, help="AF Agent Framework")
    task_commands = typer.Typer(name="tasks", no_args_is_help=True, help="Persisted Task CRUD")
    ideon_commands = typer.Typer(
        name="ideon",
        no_args_is_help=True,
        help="Deterministic Prime Language / Ideon reference",
    )
    migration_commands = typer.Typer(
        name="migrations",
        no_args_is_help=True,
        help="Plan, inspect, apply, reverse, and verify database migrations",
    )
    owns_application = application is None
    cached_application = application

    def current() -> AFApplication:
        nonlocal cached_application
        if cached_application is None:
            cached_application = create_application()
        return cached_application

    def fail(error: Exception) -> None:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2)

    @cli.callback()
    def lifecycle(context: typer.Context) -> None:
        def close_owned_application() -> None:
            nonlocal cached_application
            if owns_application and cached_application is not None:
                cached_application.shutdown()
                cached_application = None

        context.call_on_close(close_owned_application)

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

    def migration_runner() -> MigrationRunner:
        runner = current().container.resolve(MigrationRunner)
        runner.bootstrap()
        return runner

    def emit_plan(plan: MigrationPlan, *, dry_run: bool) -> None:
        typer.echo(
            json.dumps(
                {
                    "direction": plan.direction.value,
                    "migration_ids": list(plan.migration_ids),
                    "count": len(plan.migration_ids),
                    "dry_run": dry_run,
                }
            )
        )

    @migration_commands.command("plan")
    def plan_migrations(
        direction: Annotated[MigrationDirection, typer.Option()] = MigrationDirection.UP,
        limit: Annotated[int, typer.Option(min=1, max=10_000)] = 10_000,
        steps: Annotated[int, typer.Option(min=1, max=1_000)] = 1,
    ) -> None:
        """Show a bounded deterministic plan without exposing SQL."""
        try:
            runner = migration_runner()
            plan = (
                runner.plan_up(limit=limit)
                if direction is MigrationDirection.UP
                else runner.plan_down(steps=steps)
            )
            emit_plan(plan, dry_run=True)
        except (AFError, TypeError, ValueError) as error:
            fail(error)

    @migration_commands.command("status")
    def migration_status() -> None:
        """Show applied and pending migration identifiers."""
        try:
            runner = migration_runner()
            applied = runner.verify()
            pending = runner.plan_up().migration_ids
            typer.echo(json.dumps({"applied": list(applied), "pending": list(pending)}))
        except (AFError, TypeError, ValueError) as error:
            fail(error)

    @migration_commands.command("up")
    def migrate_up(
        limit: Annotated[int, typer.Option(min=1, max=10_000)] = 10_000,
        dry_run: Annotated[bool, typer.Option()] = False,
    ) -> None:
        """Apply a bounded prefix of pending migrations."""
        try:
            emit_plan(migration_runner().apply_up(limit=limit, dry_run=dry_run), dry_run=dry_run)
        except (AFError, TypeError, ValueError) as error:
            fail(error)

    @migration_commands.command("down")
    def migrate_down(
        steps: Annotated[int, typer.Option(min=1, max=1_000)] = 1,
        dry_run: Annotated[bool, typer.Option()] = False,
    ) -> None:
        """Reverse a bounded number of applied reversible migrations."""
        try:
            emit_plan(
                migration_runner().apply_down(steps=steps, dry_run=dry_run),
                dry_run=dry_run,
            )
        except (AFError, TypeError, ValueError) as error:
            fail(error)

    @migration_commands.command("verify")
    def verify_migrations() -> None:
        """Verify the ledger and every declared probe for applied migrations."""
        try:
            typer.echo(json.dumps({"verified": list(migration_runner().verify())}))
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
    cli.add_typer(migration_commands, name="migrations")
    cli.add_typer(ideon_commands, name="ideon")
    return cli


app = create_cli()
