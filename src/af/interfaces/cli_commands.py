"""Provider-contributed CLI command helpers for AF's console kernel."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

import typer

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

    try:
        contributors = application.container.resolve_tagged(CLI_COMMAND_CONTRIBUTORS_TAG)
    except ServiceNotBound:
        return
    for contributor in contributors:
        if not isinstance(contributor, CliCommandContributor):
            raise PlatformError(
                PlatformReason.CONFIGURATION_INVALID,
                stage="cli.command-contributors",
            )
        contributor.register(cli)
