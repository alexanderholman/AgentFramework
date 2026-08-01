"""Named cache manager with verified capability routing."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from time import monotonic, sleep
from types import MappingProxyType
from typing import Protocol
from uuid import uuid4

from af.cache.contracts import CacheKey, CacheNamespace, LockLease, LockOwner
from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.errors import PlatformError, PlatformReason
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


def _loader(loader: object) -> Callable[[], bytes]:
    if not callable(loader):
        raise TypeError("cache loader must be callable")
    return loader


def _duration(value: object, *, label: str) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"cache {label} must be a positive duration")
    return value


def _scheduler(schedule: object) -> Callable[[Callable[[], None]], None]:
    if not callable(schedule):
        raise TypeError("cache revalidation scheduler must be callable")
    return schedule


def _swr_marker(key: CacheKey) -> CacheKey:
    digest = sha256(key.value.encode("utf-8")).hexdigest()
    return CacheKey(f"af-swr:{digest}")


def _stale_horizon(fresh_ttl: timedelta, stale_ttl: timedelta) -> timedelta:
    try:
        return fresh_ttl + stale_ttl
    except OverflowError as exc:
        raise ValueError("cache stale horizon must be bounded") from exc


class CacheFeature(StrEnum):
    """Cache capabilities verified independently before use."""

    READ = "read"
    WRITE = "write"
    TTL = "ttl"
    NAMESPACES = "namespaces"
    FLUSH = "flush"
    LOCKS = "locks"


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

    def acquire_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        owner: LockOwner,
        /,
        *,
        ttl: timedelta,
    ) -> LockLease | None: ...

    def renew_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        lease: LockLease,
        /,
        *,
        ttl: timedelta,
    ) -> LockLease | None: ...

    def release_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        lease: LockLease,
        /,
    ) -> bool: ...

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

        if type(registration) is not DriverRegistration or registration.manager_id != "cache":
            raise ManagerConfigurationError(
                protected_cause=ValueError("driver registration does not target the cache manager")
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

    def remember(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        loader: Callable[[], bytes],
        *,
        ttl: timedelta | None = None,
        lock_ttl: timedelta | None = None,
        wait_timeout: timedelta | None = None,
        name: str | None = None,
    ) -> bytes:
        resolved = _loader(loader)
        if lock_ttl is None:
            if wait_timeout is not None:
                raise ValueError("cache wait timeout requires a lock TTL")
        else:
            lock_ttl = _duration(lock_ttl, label="lock TTL")
            wait_timeout = _duration(
                lock_ttl if wait_timeout is None else wait_timeout,
                label="wait timeout",
            )
        cached = self.get(namespace, key, name=name)
        if cached is not None:
            return cached
        if lock_ttl is not None and wait_timeout is not None:
            return self._remember_coalesced(
                namespace,
                key,
                resolved,
                ttl=ttl,
                lock_ttl=lock_ttl,
                wait_timeout=wait_timeout,
                name=name,
            )
        value = resolved()
        self.set(namespace, key, value, ttl=ttl, name=name)
        return value

    def _remember_coalesced(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        loader: Callable[[], bytes],
        *,
        ttl: timedelta | None,
        lock_ttl: timedelta,
        wait_timeout: timedelta,
        name: str | None,
    ) -> bytes:
        driver = self.connection(
            name,
            requires=(
                CacheFeature.READ,
                CacheFeature.WRITE,
                CacheFeature.TTL,
                CacheFeature.NAMESPACES,
                CacheFeature.LOCKS,
            ),
        )
        owner = LockOwner(f"remember:{uuid4().hex}")
        deadline = monotonic() + wait_timeout.total_seconds()
        while True:
            lease = driver.acquire_lock(namespace, key, owner, ttl=lock_ttl)
            if lease is not None:
                active_lease = lease
                try:
                    cached = driver.get(namespace, key)
                    if cached is not None:
                        return cached
                    value = loader()
                    renewed = driver.renew_lock(
                        namespace,
                        key,
                        active_lease,
                        ttl=lock_ttl,
                    )
                    if renewed is None:
                        cached = driver.get(namespace, key)
                        if cached is not None:
                            return cached
                        raise PlatformError(
                            PlatformReason.TRANSIENT_FAILURE,
                            stage="cache.remember.lease_lost",
                        )
                    active_lease = renewed
                    driver.set(namespace, key, value, ttl=ttl)
                    return value
                finally:
                    driver.release_lock(namespace, key, active_lease)

            cached = driver.get(namespace, key)
            if cached is not None:
                return cached
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise PlatformError(
                    PlatformReason.TRANSIENT_FAILURE,
                    stage="cache.remember.wait_timeout",
                )
            sleep(min(0.01, remaining))

    def stale_while_revalidate(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        loader: Callable[[], bytes],
        *,
        fresh_ttl: timedelta,
        stale_ttl: timedelta,
        lock_ttl: timedelta,
        schedule: Callable[[Callable[[], None]], None],
        wait_timeout: timedelta | None = None,
        name: str | None = None,
    ) -> bytes:
        """Serve fresh or retained stale data while one owner schedules refresh."""

        resolved = _loader(loader)
        fresh_ttl = _duration(fresh_ttl, label="fresh TTL")
        stale_ttl = _duration(stale_ttl, label="stale TTL")
        lock_ttl = _duration(lock_ttl, label="lock TTL")
        wait_timeout = _duration(
            lock_ttl if wait_timeout is None else wait_timeout,
            label="wait timeout",
        )
        scheduler = _scheduler(schedule)
        hard_ttl = _stale_horizon(fresh_ttl, stale_ttl)
        driver = self.connection(
            name,
            requires=(
                CacheFeature.READ,
                CacheFeature.WRITE,
                CacheFeature.TTL,
                CacheFeature.NAMESPACES,
                CacheFeature.LOCKS,
            ),
        )
        marker = _swr_marker(key)
        cached = driver.get(namespace, key)
        if cached is None:
            return self._swr_cold_fill(
                driver,
                namespace,
                key,
                marker,
                resolved,
                fresh_ttl=fresh_ttl,
                hard_ttl=hard_ttl,
                lock_ttl=lock_ttl,
                wait_timeout=wait_timeout,
            )
        if driver.get(namespace, marker) is not None:
            return cached

        owner = LockOwner(f"swr:{uuid4().hex}")
        lease = driver.acquire_lock(namespace, key, owner, ttl=lock_ttl)
        if lease is None:
            return cached

        def refresh() -> None:
            active_lease = lease
            try:
                try:
                    value = resolved()
                except Exception:
                    return
                renewed = driver.renew_lock(
                    namespace,
                    key,
                    active_lease,
                    ttl=lock_ttl,
                )
                if renewed is None:
                    return
                active_lease = renewed
                driver.set(namespace, key, value, ttl=hard_ttl)
                driver.set(namespace, marker, b"fresh", ttl=fresh_ttl)
            finally:
                driver.release_lock(namespace, key, active_lease)

        try:
            scheduler(refresh)
        except Exception:
            driver.release_lock(namespace, key, lease)
        return cached

    def _swr_cold_fill(
        self,
        driver: CacheDriver,
        namespace: CacheNamespace,
        key: CacheKey,
        marker: CacheKey,
        loader: Callable[[], bytes],
        *,
        fresh_ttl: timedelta,
        hard_ttl: timedelta,
        lock_ttl: timedelta,
        wait_timeout: timedelta,
    ) -> bytes:
        owner = LockOwner(f"swr-cold:{uuid4().hex}")
        deadline = monotonic() + wait_timeout.total_seconds()
        while True:
            lease = driver.acquire_lock(namespace, key, owner, ttl=lock_ttl)
            if lease is not None:
                active_lease = lease
                try:
                    cached = driver.get(namespace, key)
                    if cached is not None:
                        return cached
                    value = loader()
                    renewed = driver.renew_lock(
                        namespace,
                        key,
                        active_lease,
                        ttl=lock_ttl,
                    )
                    if renewed is None:
                        cached = driver.get(namespace, key)
                        if cached is not None:
                            return cached
                        raise PlatformError(
                            PlatformReason.TRANSIENT_FAILURE,
                            stage="cache.swr.lease_lost",
                        )
                    active_lease = renewed
                    driver.set(namespace, key, value, ttl=hard_ttl)
                    driver.set(namespace, marker, b"fresh", ttl=fresh_ttl)
                    return value
                finally:
                    driver.release_lock(namespace, key, active_lease)

            cached = driver.get(namespace, key)
            if cached is not None:
                return cached
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise PlatformError(
                    PlatformReason.TRANSIENT_FAILURE,
                    stage="cache.swr.wait_timeout",
                )
            sleep(min(0.01, remaining))

    def memoize(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        loader: Callable[[], bytes],
        *,
        ttl: timedelta | None = None,
        lock_ttl: timedelta | None = None,
        wait_timeout: timedelta | None = None,
        name: str | None = None,
    ) -> Callable[[], bytes]:
        resolved = _loader(loader)

        def cached() -> bytes:
            return self.remember(
                namespace,
                key,
                resolved,
                ttl=ttl,
                lock_ttl=lock_ttl,
                wait_timeout=wait_timeout,
                name=name,
            )

        return cached

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

    def acquire_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        owner: LockOwner,
        *,
        ttl: timedelta,
        name: str | None = None,
    ) -> LockLease | None:
        return self.connection(
            name,
            requires=(
                CacheFeature.LOCKS,
                CacheFeature.NAMESPACES,
                CacheFeature.TTL,
            ),
        ).acquire_lock(namespace, key, owner, ttl=ttl)

    def renew_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        lease: LockLease,
        *,
        ttl: timedelta,
        name: str | None = None,
    ) -> LockLease | None:
        return self.connection(
            name,
            requires=(
                CacheFeature.LOCKS,
                CacheFeature.NAMESPACES,
                CacheFeature.TTL,
            ),
        ).renew_lock(namespace, key, lease, ttl=ttl)

    def release_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        lease: LockLease,
        *,
        name: str | None = None,
    ) -> bool:
        return self.connection(
            name,
            requires=(CacheFeature.LOCKS, CacheFeature.NAMESPACES),
        ).release_lock(namespace, key, lease)


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
            type(registration) is not DriverRegistration or registration.manager_id != "cache"
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
