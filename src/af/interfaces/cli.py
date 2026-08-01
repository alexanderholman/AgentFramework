"""Typer console kernel: AF's Artisan-like operator interface."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import suppress
from importlib import import_module
from inspect import iscoroutine
from pathlib import Path
from typing import Annotated, cast

import typer
from typer._click.core import Command, Context
from typer.core import TyperGroup

from af.application.ideons import InterpretRequest
from af.application.version_control import VersionControlController
from af.bootstrap import AFApplication, create_application
from af.bootstrap_version_control import create_version_control_controller
from af.domain.ideons.records import ContextGraph
from af.framework.container import Container, InvocationKind
from af.framework.context import AcceptanceScope, ExecutionContext
from af.framework.errors import AFError, PlatformError, PlatformReason
from af.framework.migrations import MigrationDirection, MigrationPlan, MigrationRunner
from af.framework.version_control import VersionControlRequest
from af.interfaces.cli_commands import (
    install_cli_command_contributors,
    install_click_group_command_contributors,
)
from af.interfaces.cli_interactions import CliInteraction, NonInteractiveCliInteraction
from af.interfaces.presenters import (
    agent_route_to_dict,
    delete_to_dict,
    search_index_result_to_dict,
    search_result_to_dict,
    task_to_dict,
)

_APPLICATION_FACTORY = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:"
    r"[A-Za-z_][A-Za-z0-9_]*$"
)
_MAX_APPLICATION_FACTORY_LENGTH = 256
_CURRENT_APPLICATION_META_KEY = "af.current_application"
_CURRENT_CONTAINER_META_KEY = "af.current_container"
_CURRENT_INTERACTION_META_KEY = "af.current_interaction"


def _validate_application_factory(value: str) -> None:
    if (
        type(value) is not str
        or len(value) > _MAX_APPLICATION_FACTORY_LENGTH
        or _APPLICATION_FACTORY.fullmatch(value) is None
    ):
        raise PlatformError(
            PlatformReason.CONFIGURATION_INVALID,
            stage="cli.application-factory",
        )


def _load_application_factory(value: str) -> Callable[[], object]:
    _validate_application_factory(value)
    module_name, attribute = value.split(":", maxsplit=1)
    try:
        candidate = getattr(import_module(module_name), attribute)
    except Exception as error:
        raise PlatformError(
            PlatformReason.CONFIGURATION_INVALID,
            stage="cli.application-factory",
            protected_cause=error,
        ) from None
    if not callable(candidate):
        raise PlatformError(
            PlatformReason.CONFIGURATION_INVALID,
            stage="cli.application-factory",
        )
    return cast(Callable[[], object], candidate)


def current_application_from_context(context: typer.Context) -> AFApplication:
    current = context.meta.get(_CURRENT_APPLICATION_META_KEY)
    if not callable(current):
        raise PlatformError(
            PlatformReason.CONTEXT_UNAVAILABLE,
            stage="cli.application-context",
        )
    application = cast(AFApplication, current())
    if not isinstance(application, AFApplication):
        raise PlatformError(
            PlatformReason.CONTEXT_UNAVAILABLE,
            stage="cli.application-context",
        )
    return application


def current_container_from_context(context: typer.Context) -> Container:
    current = context.meta.get(_CURRENT_CONTAINER_META_KEY)
    if not callable(current):
        raise PlatformError(
            PlatformReason.CONTEXT_UNAVAILABLE,
            stage="cli.container-context",
        )
    container = current()
    if not isinstance(container, Container):
        raise PlatformError(
            PlatformReason.CONTEXT_UNAVAILABLE,
            stage="cli.container-context",
        )
    return container


def current_interaction_from_context(context: typer.Context) -> CliInteraction:
    current = context.meta.get(_CURRENT_INTERACTION_META_KEY)
    if not callable(current):
        raise PlatformError(
            PlatformReason.CONTEXT_UNAVAILABLE,
            stage="cli.interaction-context",
        )
    interaction = current()
    if not isinstance(interaction, CliInteraction):
        raise PlatformError(
            PlatformReason.CONTEXT_UNAVAILABLE,
            stage="cli.interaction-context",
        )
    return interaction


def create_cli(
    application: AFApplication | None = None,
    *,
    application_factory: Callable[[], AFApplication] | None = None,
    interaction_factory: Callable[[], CliInteraction] | None = None,
    version_control: VersionControlController | None = None,
) -> typer.Typer:
    task_commands = typer.Typer(name="tasks", no_args_is_help=True, help="Persisted Task CRUD")
    ideon_commands = typer.Typer(
        name="ideon",
        no_args_is_help=True,
        help="Deterministic Prime Language / Ideon reference",
    )
    search_commands = typer.Typer(
        name="search",
        no_args_is_help=True,
        help="Index and query persisted AF history",
    )
    migration_commands = typer.Typer(
        name="migrations",
        no_args_is_help=True,
        help="Plan, inspect, apply, reverse, and verify database migrations",
    )
    initialization_commands = typer.Typer(
        name="init",
        no_args_is_help=True,
        help="Initialize AF's local operating foundations",
    )
    version_commands = typer.Typer(
        name="version",
        no_args_is_help=True,
        help="Initialize version-control foundations",
    )
    if application is not None and application_factory is not None:
        raise ValueError("provide application or application_factory, not both")
    owns_application = application is None
    cached_application = application
    cached_version_control = version_control
    configured_factory: Callable[[], object] = (
        create_application if application_factory is None else application_factory
    )
    selected_factory_path: str | None = None
    lifecycle_context: typer.Context | Context | None = None
    current_invocation: Container | None = None
    current_interaction: CliInteraction | None = None
    accepts_operator_factory = application is None and application_factory is None

    class _AFRootGroup(TyperGroup):
        def get_command(self, ctx: Context, name: str) -> Command | None:
            nonlocal selected_factory_path
            command = super().get_command(ctx, name)
            if command is not None or ctx.resilient_parsing:
                return command
            if accepts_operator_factory:
                selected_factory_path = cast(str | None, ctx.params.get("factory_path"))
            install_click_group_command_contributors(self, current())
            return super().get_command(ctx, name)

    cli = typer.Typer(
        name="af",
        no_args_is_help=True,
        help="AF Agent Framework",
        cls=_AFRootGroup,
    )

    def current_container() -> Container:
        application = current()
        if current_invocation is None:
            if isinstance(application, AFApplication):
                return application.container
            raise PlatformError(
                PlatformReason.CONTEXT_UNAVAILABLE,
                stage="cli.container-context",
            )
        return current_invocation

    def current() -> AFApplication:
        nonlocal cached_application, current_invocation
        if cached_application is None:
            factory = configured_factory
            external_factory = application_factory is not None
            if selected_factory_path is not None:
                factory = _load_application_factory(selected_factory_path)
                external_factory = True
            try:
                resolved = factory()
            except Exception as error:
                if not external_factory and isinstance(error, AFError):
                    raise
                raise PlatformError(
                    PlatformReason.CONFIGURATION_INVALID,
                    stage="cli.application-factory",
                    protected_cause=error,
                ) from None
            if external_factory:
                if not isinstance(resolved, AFApplication):
                    if iscoroutine(resolved):
                        try:
                            resolved.close()
                        except Exception as error:
                            with suppress(Exception):
                                resolved.close()
                            raise PlatformError(
                                PlatformReason.CONFIGURATION_INVALID,
                                stage="cli.application-factory",
                                protected_cause=error,
                            ) from None
                    raise PlatformError(
                        PlatformReason.CONFIGURATION_INVALID,
                        stage="cli.application-factory",
                    )
                cached_application = resolved
            else:
                cached_application = cast(AFApplication, resolved)
        if (
            isinstance(cached_application, AFApplication)
            and lifecycle_context is not None
            and current_invocation is None
        ):
            current_invocation = lifecycle_context.with_resource(
                cached_application.container.invocation_scope(
                    context=ExecutionContext(
                        requested_scope=AcceptanceScope.DETERMINISTIC,
                        effective_scope=AcceptanceScope.DETERMINISTIC,
                    ),
                    kind=InvocationKind.INTERACTION,
                )
            )
        return cached_application

    def interaction() -> CliInteraction:
        nonlocal current_interaction
        if current_interaction is None:
            factory = interaction_factory or NonInteractiveCliInteraction
            current_interaction = factory()
        return current_interaction

    def current_version_control() -> VersionControlController:
        nonlocal cached_version_control
        if cached_version_control is None:
            cached_version_control = create_version_control_controller()
        return cached_version_control

    def fail(error: Exception) -> None:
        typer.echo(str(error), err=True)
        raise typer.Exit(code=2)

    @cli.callback()
    def lifecycle(
        context: typer.Context,
        factory_path: Annotated[
            str | None,
            typer.Option(
                "--application-factory",
                help="Explicit project application factory as module:callable",
            ),
        ] = None,
    ) -> None:
        nonlocal lifecycle_context, selected_factory_path
        lifecycle_context = context
        selected_factory_path = None
        context.meta[_CURRENT_APPLICATION_META_KEY] = current
        context.meta[_CURRENT_CONTAINER_META_KEY] = current_container
        context.meta[_CURRENT_INTERACTION_META_KEY] = interaction
        if factory_path is not None:
            if not accepts_operator_factory:
                fail(
                    PlatformError(
                        PlatformReason.CONFIGURATION_INVALID,
                        stage="cli.application-factory",
                    )
                )
            try:
                _validate_application_factory(factory_path)
                selected_factory_path = factory_path
            except (AFError, TypeError, ValueError) as error:
                fail(error)

        def close_owned_application() -> None:
            nonlocal cached_application
            nonlocal current_invocation
            nonlocal current_interaction
            nonlocal lifecycle_context
            nonlocal selected_factory_path
            try:
                if owns_application and cached_application is not None:
                    cached_application.shutdown()
            finally:
                current_invocation = None
                current_interaction = None
                lifecycle_context = None
                if owns_application:
                    cached_application = None
                selected_factory_path = None

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

    @search_commands.command("index")
    def index_search(
        limit: Annotated[int, typer.Option(min=1, max=1_000)] = 100,
    ) -> None:
        try:
            typer.echo(json.dumps(search_index_result_to_dict(current().search.index(limit=limit))))
        except AFError as error:
            fail(error)

    @search_commands.command("query")
    def query_search(
        text: Annotated[str, typer.Argument(help="Bounded full-text query")],
        limit: Annotated[int, typer.Option(min=1, max=1_000)] = 20,
    ) -> None:
        try:
            result = current().search.search(text, limit=limit)
            typer.echo(json.dumps(search_result_to_dict(result)))
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
        if accepts_operator_factory and selected_factory_path is None:
            raise PlatformError(
                PlatformReason.CONFIGURATION_INVALID,
                stage="cli.application-factory",
            )
        return current().container.resolve(MigrationRunner)

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

    @migration_commands.command("bootstrap")
    def bootstrap_migrations() -> None:
        """Provision the fixed migration metadata tables explicitly."""
        try:
            migration_runner().bootstrap()
            typer.echo(json.dumps({"bootstrapped": True}))
        except (AFError, TypeError, ValueError) as error:
            fail(error)

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

    @version_commands.command("control")
    def initialize_version_control(
        upstream_remote: Annotated[
            str | None,
            typer.Option(
                "--upstream-remote",
                help="Authoritative existing remote; required when discovery is ambiguous",
            ),
        ] = None,
        coordination_remote: Annotated[
            str,
            typer.Option(
                "--coordination-remote",
                help="Managed local bare-remote name",
            ),
        ] = "af-local",
    ) -> None:
        """Establish private local coordination for isolated AF branches."""
        try:
            result = current_version_control().initialize(
                VersionControlRequest(
                    cwd=Path.cwd(),
                    upstream_remote=upstream_remote,
                    coordination_remote=coordination_remote,
                )
            )
            typer.echo(
                json.dumps(
                    result.to_dict(),
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        except (AFError, TypeError, ValueError) as error:
            fail(error)

    @cli.command()
    def serve(
        host: Annotated[str | None, typer.Option()] = None,
        port: Annotated[int | None, typer.Option()] = None,
        allowed_host: Annotated[list[str] | None, typer.Option("--allowed-host")] = None,
        web_profile: Annotated[str, typer.Option("--web-profile")] = "basic",
        experimental_web_features: Annotated[
            bool,
            typer.Option("--experimental-web-features"),
        ] = False,
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
                web_profile=web_profile,
                enable_experimental_web_features=experimental_web_features,
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

    initialization_commands.add_typer(version_commands, name="version")
    cli.add_typer(initialization_commands, name="init")
    cli.add_typer(search_commands, name="search")
    cli.add_typer(task_commands, name="tasks")
    cli.add_typer(migration_commands, name="migrations")
    cli.add_typer(ideon_commands, name="ideon")
    if isinstance(application, AFApplication):
        install_cli_command_contributors(cli, application)
    return cli


app = create_cli()
