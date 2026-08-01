"""Named cache manager with verified capability routing."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from af.cache.contracts import CacheKey, CacheNamespace
from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    Manager,
    ManagerConfigurationError,
)
from af.observability.signals import TelemetryPublisher

__all__ = (
    "CacheDriver",
    "CacheFeature",
    "CacheManager",
    "bind_cache_manager",
)


class CacheFeature(StrEnum):
    """Cache capabilities verified independently before use."""

    READ = "read"
    WRITE = "write"
    TTL = "ttl"
    NAMESPACES = "namespaces"
    FLUSH = "flush"


class CacheDriver(Protocol):
    """Minimum synchronous cache backend contract owned by CacheManager."""

    def get(self, namespace: CacheNamespace, key: CacheKey, /) -> bytes | None: ...

    def set(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        value: bytes,
        /,
        *,
        ttl: timedelta | None = None,
    ) -> None: ...

    def delete(self, namespace: CacheNamespace, key: CacheKey, /) -> bool: ...

    def flush(self, namespace: CacheNamespace, /) -> int: ...

    def close(self) -> None: ...


def _capabilities(
    features: CapabilitySet | Iterable[str | CacheFeature],
) -> CapabilitySet:
    if isinstance(features, CapabilitySet):
        return features
    if isinstance(features, str):
        return CapabilitySet((features,))
    values: list[str] = []
    for feature in tuple(features):
        if isinstance(feature, CacheFeature):
            values.append(feature.value)
        elif type(feature) is str:
            values.append(feature)
        else:
            raise ValueError("cache requirements must be capability identifiers")
    return CapabilitySet(values)


class CacheManager(Manager[CacheDriver]):
    """Application-owned named cache connections."""

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
            manager_id="cache",
            connections=connections,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )

    def install(self, registration: DriverRegistration[CacheDriver]) -> None:
        """Install one inert cache registration."""

        if (
            type(registration) is not DriverRegistration
            or registration.manager_id != "cache"
        ):
            raise ManagerConfigurationError(
                protected_cause=ValueError(
                    "driver registration does not target the cache manager"
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
        requires: CapabilitySet | Iterable[str | CacheFeature] = (),
    ) -> CacheDriver:
        return super().connection(name, requires=_capabilities(requires))

    def get(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        *,
        name: str | None = None,
    ) -> bytes | None:
        return self.connection(
            name,
            requires=(CacheFeature.READ, CacheFeature.NAMESPACES),
        ).get(namespace, key)

    def set(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        value: bytes,
        *,
        ttl: timedelta | None = None,
        name: str | None = None,
    ) -> None:
        required = [CacheFeature.WRITE, CacheFeature.NAMESPACES]
        if ttl is not None:
            required.append(CacheFeature.TTL)
        self.connection(name, requires=required).set(
            namespace,
            key,
            value,
            ttl=ttl,
        )

    def delete(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        *,
        name: str | None = None,
    ) -> bool:
        return self.connection(
            name,
            requires=(CacheFeature.WRITE, CacheFeature.NAMESPACES),
        ).delete(namespace, key)

    def flush(
        self,
        namespace: CacheNamespace,
        *,
        name: str | None = None,
    ) -> int:
        return self.connection(
            name,
            requires=(CacheFeature.FLUSH, CacheFeature.NAMESPACES),
        ).flush(namespace)


def bind_cache_manager(
    registrar: ProviderRegistrar,
    *,
    connections: Mapping[str, object],
    default: str | None,
    deployment_profile: str,
    policy_version: str,
    registrations: Iterable[DriverRegistration[CacheDriver]],
    clock: Callable[[], datetime] | None = None,
    telemetry: TelemetryPublisher | None = None,
) -> None:
    """Bind one application-owned CacheManager from inert registrations."""

    snapshot = tuple(registrations)
    if (
        not snapshot
        or any(
            type(registration) is not DriverRegistration
            or registration.manager_id != "cache"
            for registration in snapshot
        )
        or len({registration.driver_name for registration in snapshot}) != len(snapshot)
    ):
        raise ManagerConfigurationError(
            protected_cause=ValueError(
                "cache registrations must be non-empty, unique, and targeted"
            )
        )

    template = CacheManager(
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

    def create_manager(_resolver: Resolver) -> CacheManager:
        manager = CacheManager(
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
        CacheManager,
        create_manager,
        scope=Scope.APPLICATION,
        owned=True,
        replace=False,
    )
