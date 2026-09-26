"""Opt-in local engineering composition; repository paths are host configuration."""

from __future__ import annotations

from pathlib import Path

from af.application.engineering import LocalEngineeringService
from af.application.engineering_probe import InstalledEngineeringRouteProbe
from af.application.engineering_routes import engineering_contribution
from af.domain.classifiers import ClassifierChain
from af.framework.container import (
    Container,
    ProviderRegistrar,
    ProviderResourceRegistry,
    Scope,
    ServiceProvider,
)
from af.framework.procedure_contributors import PROCEDURE_CONTRIBUTORS_TAG
from af.framework.routing import ProcedureRegistry
from af.infrastructure.database import Database
from af.infrastructure.local_engineering import create_local_engineering_service
from af.infrastructure.local_keys import LocalAuthenticationKey


class LocalEngineeringProvider(ServiceProvider):
    provider_id = "af.local-engineering"
    dependencies = ("af.framework",)

    def __init__(self, repository: Path, *, selector_configured: bool) -> None:
        self._repository, self._selector = repository, selector_configured
        self._container: Container | None = None

    def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
        del resources
        self._container = container

    def shutdown(self, container: Container) -> None:
        del container
        self._container = None

    def _classifiers(self) -> ClassifierChain:
        if self._container is None:
            raise ValueError("engineering routing scope is inactive")
        return self._container.resolve(ClassifierChain)

    def _registry(self) -> ProcedureRegistry:
        if self._container is None:
            raise ValueError("engineering routing scope is inactive")
        return self._container.resolve(ProcedureRegistry)

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            LocalEngineeringService,
            lambda current: create_local_engineering_service(
                current.resolve(Database),
                self._repository,
                authentication_key=current.resolve(LocalAuthenticationKey).value,
                route_probe=InstalledEngineeringRouteProbe(
                    self._classifiers,
                    self._registry,
                    selector_configured=self._selector,
                ),
            ),
            scope=Scope.APPLICATION,
        )
        contribution = object()
        registrar.bind(
            contribution,
            lambda current: engineering_contribution(current.resolve(LocalEngineeringService)),
            scope=Scope.APPLICATION,
        )
        registrar.tag(PROCEDURE_CONTRIBUTORS_TAG, contribution)
