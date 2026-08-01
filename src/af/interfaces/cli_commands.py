"""Provider-contributed CLI command helpers for AF's console kernel."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import typer
from typer.core import TyperCommand, TyperGroup
from typer.main import get_command as get_typer_command

from af.framework.container import ProviderRegistrar, Scope
from af.framework.errors import PlatformError, PlatformReason, ServiceNotBound

if TYPE_CHECKING:
    from af.bootstrap import AFApplication

CLI_COMMAND_CONTRIBUTORS_TAG = "af.cli.command-contributors"


@runtime_checkable
class CliCommandContributor(Protocol):
    """One provider-installed CLI command contribution."""

    def register(self, cli: typer.Typer) -> None: ...


def bind_cli_command_contributor(
    registrar: ProviderRegistrar,
    contributor: CliCommandContributor,
) -> None:
    """Bind one contributor and append it to the shared CLI discovery tag."""

    token = object()
    registrar.bind(token, lambda _: contributor, scope=Scope.APPLICATION, owned=False)
    registrar.tag(CLI_COMMAND_CONTRIBUTORS_TAG, token)


def install_cli_command_contributors(cli: typer.Typer, application: AFApplication) -> None:
    """Install every command contributor already present in an AF application."""

    for contributor in _resolve_cli_command_contributors(application):
        contributor.register(cli)


def install_click_group_command_contributors(
    group: TyperGroup,
    application: AFApplication,
) -> None:
    """Install provider-contributed commands into one invocation-local Click group."""

    for contributor in _resolve_cli_command_contributors(application):
        contributed = typer.Typer(add_completion=False)
        contributor.register(contributed)
        command = get_typer_command(contributed)
        if isinstance(command, TyperGroup):
            for name, child in command.commands.items():
                group.add_command(child, name)
            continue
        if not isinstance(command, TyperCommand):
            raise PlatformError(
                PlatformReason.CONFIGURATION_INVALID,
                stage="cli.command-contributors",
            )
        group.add_command(command, command.name)


def _resolve_cli_command_contributors(
    application: AFApplication,
) -> tuple[CliCommandContributor, ...]:
    """Return validated CLI contributors from an AF application container."""

    try:
        contributors = application.container.resolve_tagged(CLI_COMMAND_CONTRIBUTORS_TAG)
    except ServiceNotBound:
        return ()
    validated: list[CliCommandContributor] = []
    for contributor in contributors:
        if not isinstance(contributor, CliCommandContributor):
            raise PlatformError(
                PlatformReason.CONFIGURATION_INVALID,
                stage="cli.command-contributors",
            )
        validated.append(contributor)
    return tuple(validated)
