"""Specialized named relational driver manager and provider binding."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from datetime import datetime
from types import MappingProxyType

from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    Manager,
    ManagerConfigurationError,
)
from af.framework.relational import (
    DatabaseDriver,
    DatabaseFeature,
    DatabaseTransaction,
)
from af.framework.unit_of_work import UnitOfWork
from af.observability.signals import TelemetryPublisher

__all__ = ("DatabaseManager", "bind_database_manager")


def _capabilities(
    features: CapabilitySet | Iterable[str | DatabaseFeature],
) -> CapabilitySet:
    if isinstance(features, CapabilitySet):
        return features
    if isinstance(features, str):
        return CapabilitySet((features,))
    snapshot = tuple(features)
    values: list[str] = []
    for feature in snapshot:
        if isinstance(feature, DatabaseFeature):
            values.append(feature.value)
        elif type(feature) is str:
            values.append(feature)
        else:
            raise ValueError("database requirements must be capability identifiers")
    return CapabilitySet(values)


class DatabaseManager(Manager[DatabaseDriver]):
    """Named database connections with explicit read/write routing."""

    def __init__(
        self,
        *,
        connections: Mapping[str, object],
        default: str | None,
        read: str | None,
        write: str | None,
        deployment_profile: str,
        policy_version: str,
        clock: Callable[[], datetime] | None = None,
        telemetry: TelemetryPublisher | None = None,
    ) -> None:
        super().__init__(
            manager_id="database",
            connections=connections,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )
        self._read = default if read is None else read
        self._write = default if write is None else write
        for name in (self._read, self._write):
            if name is not None and name not in self._definitions:
                raise ManagerConfigurationError(
                    protected_cause=LookupError("database route references an unknown connection")
                )

    def install(self, registration: DriverRegistration[DatabaseDriver]) -> None:
        """Install one inert registration declared for the database manager."""

        if type(registration) is not DriverRegistration or registration.manager_id != "database":
            raise ManagerConfigurationError(
                protected_cause=ValueError(
                    "driver registration does not target the database manager"
                )
            )
        self.extend(
            registration.driver_name,
            registration.factory,
            manifest=registration.manifest,
            verifier=registration.verifier,
            disposer=registration.disposer,
        )

    def connection(
        self,
        name: str | None = None,
        *,
        requires: CapabilitySet | Iterable[str | DatabaseFeature] = (),
    ) -> DatabaseDriver:
        return super().connection(
            name,
            requires=_capabilities(requires),
        )

    def transaction(
        self,
        name: str | None = None,
        *,
        read_only: bool = False,
        requires: Iterable[DatabaseFeature] = (),
    ) -> AbstractContextManager[DatabaseTransaction]:
        """Open a routed transaction only after capability verification."""

        if type(read_only) is not bool:
            raise ValueError("database transaction read-only flag must be boolean")
        target = name
        if target is None:
            target = self._read if read_only else self._write
        required = {
            DatabaseFeature.TRANSACTIONS,
            *tuple(requires),
        }
        driver = self.connection(target, requires=required)
        return driver.transaction(read_only=read_only)

    def unit_of_work(
        self,
        name: str | None = None,
        *,
        read_only: bool = False,
        requires: Iterable[DatabaseFeature] = (),
        max_after_commit_callbacks: int = 1_024,
    ) -> UnitOfWork:
        """Create one routed unit of work without entering or executing it."""

        return UnitOfWork(
            self.transaction(
                name,
                read_only=read_only,
                requires=requires,
            ),
            max_after_commit_callbacks=max_after_commit_callbacks,
        )


def bind_database_manager(
    registrar: ProviderRegistrar,
    *,
    connections: Mapping[str, object],
    default: str | None,
    read: str | None,
    write: str | None,
    deployment_profile: str,
    policy_version: str,
    registrations: Iterable[DriverRegistration[DatabaseDriver]],
    clock: Callable[[], datetime] | None = None,
    telemetry: TelemetryPublisher | None = None,
) -> None:
    """Bind one application-owned DatabaseManager from inert registrations."""

    snapshot = tuple(registrations)
    if (
        not snapshot
        or any(
            type(registration) is not DriverRegistration or registration.manager_id != "database"
            for registration in snapshot
        )
        or len({registration.driver_name for registration in snapshot}) != len(snapshot)
    ):
        raise ManagerConfigurationError(
            protected_cause=ValueError(
                "database registrations must be non-empty, unique, and targeted"
            )
        )

    template = DatabaseManager(
        connections=connections,
        default=default,
        read=read,
        write=write,
        deployment_profile=deployment_profile,
        policy_version=policy_version,
        clock=clock,
    )
    connection_snapshot: Mapping[str, object] = MappingProxyType(
        {name: definition[1] for name, definition in template._definitions.items()}
    )
    template.close()

    def create_manager(_resolver: Resolver) -> DatabaseManager:
        manager = DatabaseManager(
            connections=connection_snapshot,
            default=default,
            read=read,
            write=write,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )
        for registration in snapshot:
            manager.install(registration)
        return manager

    registrar.bind(
        DatabaseManager,
        create_manager,
        scope=Scope.APPLICATION,
        owned=True,
        replace=False,
    )
