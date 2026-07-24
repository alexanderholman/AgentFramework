"""Named search driver manager with verified capability routing."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    Manager,
    ManagerConfigurationError,
)
from af.observability.signals import TelemetryPublisher
from af.search.contracts import ResourceEnvelope, SearchHit, SearchQuery

__all__ = (
    "SearchDriver",
    "SearchFeature",
    "SearchManager",
    "bind_search_manager",
)


class SearchFeature(StrEnum):
    """Capabilities independently verified before search operations."""

    POLICY_PUSHDOWN = "policy-pushdown"
    FULL_TEXT = "full-text"
    HIGHLIGHTING = "highlighting"
    INDEXING = "indexing"
    TOMBSTONES = "tombstones"
    PERSISTENCE = "persistence"
    CANCELLATION = "cancellation"


class SearchDriver(Protocol):
    """Minimum synchronous backend contract owned by SearchManager."""

    def upsert(self, resources: tuple[ResourceEnvelope, ...], /) -> int: ...

    def search(self, query: SearchQuery, /) -> tuple[SearchHit, ...]: ...

    def delete(self, *, tenant_id: str, source_id: str, resource_id: str) -> bool: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...


def _capabilities(
    features: CapabilitySet | Iterable[str | SearchFeature],
) -> CapabilitySet:
    if isinstance(features, CapabilitySet):
        return features
    if isinstance(features, str):
        return CapabilitySet((features,))
    values: list[str] = []
    for feature in tuple(features):
        if isinstance(feature, SearchFeature):
            values.append(feature.value)
        elif type(feature) is str:
            values.append(feature)
        else:
            raise ValueError("search requirements must be capability identifiers")
    return CapabilitySet(values)


class SearchManager(Manager[SearchDriver]):
    """Application-owned named search connections."""

    def __init__(
        self,
        *,
        connections: Mapping[str, object],
        default: str | None,
        deployment_profile: str,
        policy_version: str,
        clock: Callable[[], datetime] | None = None,
        telemetry: TelemetryPublisher | None = None,
    ) -> None:
        super().__init__(
            manager_id="search",
            connections=connections,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )

    def install(self, registration: DriverRegistration[SearchDriver]) -> None:
        """Install one inert search registration."""

        if type(registration) is not DriverRegistration or registration.manager_id != "search":
            raise ManagerConfigurationError(
                protected_cause=ValueError(
                    "driver registration does not target the search manager"
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
        requires: CapabilitySet | Iterable[str | SearchFeature] = (),
    ) -> SearchDriver:
        return super().connection(name, requires=_capabilities(requires))

    def search(
        self,
        query: SearchQuery,
        *,
        name: str | None = None,
    ) -> tuple[SearchHit, ...]:
        driver = self.connection(
            name,
            requires=(SearchFeature.POLICY_PUSHDOWN, SearchFeature.FULL_TEXT),
        )
        return driver.search(query)

    def upsert(
        self,
        resources: tuple[ResourceEnvelope, ...],
        *,
        name: str | None = None,
    ) -> int:
        driver = self.connection(name, requires=(SearchFeature.INDEXING,))
        return driver.upsert(resources)

    def delete(
        self,
        *,
        tenant_id: str,
        source_id: str,
        resource_id: str,
        name: str | None = None,
    ) -> bool:
        driver = self.connection(name, requires=(SearchFeature.TOMBSTONES,))
        return driver.delete(
            tenant_id=tenant_id,
            source_id=source_id,
            resource_id=resource_id,
        )


def bind_search_manager(
    registrar: ProviderRegistrar,
    *,
    connections: Mapping[str, object],
    default: str | None,
    deployment_profile: str,
    policy_version: str,
    registrations: Iterable[DriverRegistration[SearchDriver]],
    clock: Callable[[], datetime] | None = None,
    telemetry: TelemetryPublisher | None = None,
) -> None:
    """Bind one application-owned SearchManager from inert registrations."""

    snapshot = tuple(registrations)
    if (
        not snapshot
        or any(
            type(registration) is not DriverRegistration
            or registration.manager_id != "search"
            for registration in snapshot
        )
        or len({registration.driver_name for registration in snapshot}) != len(snapshot)
    ):
        raise ManagerConfigurationError(
            protected_cause=ValueError(
                "search registrations must be non-empty, unique, and targeted"
            )
        )

    template = SearchManager(
        connections=connections,
        default=default,
        deployment_profile=deployment_profile,
        policy_version=policy_version,
        clock=clock,
    )
    connection_snapshot: Mapping[str, object] = MappingProxyType(
        {name: definition[1] for name, definition in template._definitions.items()}
    )
    template.close()

    def create_manager(_resolver: Resolver) -> SearchManager:
        manager = SearchManager(
            connections=connection_snapshot,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )
        for registration in snapshot:
            manager.install(registration)
        return manager

    registrar.bind(
        SearchManager,
        create_manager,
        scope=Scope.APPLICATION,
        owned=True,
        replace=False,
    )
