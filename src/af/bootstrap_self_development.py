"""Explicit host admission of one finite local self-development work unit."""

from __future__ import annotations

from pathlib import Path

from af.application.engineering import LocalEngineeringService
from af.application.self_development import SelfDevelopmentController
from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.infrastructure.database import Database
from af.infrastructure.local_keys import LocalAuthenticationKey
from af.infrastructure.self_development import create_local_development_controller


class LocalSelfDevelopmentProvider(ServiceProvider):
    provider_id = "af.local-self-development"
    dependencies = ("af.local-engineering",)

    def __init__(self, repository: Path, unit_path: Path) -> None:
        self._repository, self._unit_path = repository, unit_path

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            SelfDevelopmentController,
            lambda current: create_local_development_controller(
                current.resolve(Database),
                self._repository,
                self._unit_path,
                authentication_key=current.resolve(LocalAuthenticationKey).value,
                engineering=current.resolve(LocalEngineeringService),
            ),
            scope=Scope.APPLICATION,
        )
