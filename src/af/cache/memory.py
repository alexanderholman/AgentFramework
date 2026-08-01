"""Verified, tenant-isolated in-memory cache driver."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock

from af.cache.contracts import (
    CacheKey,
    CacheNamespace,
    _validated_operation,
    _validated_ttl,
    _validated_value,
)
from af.cache.manager import CacheDriver, CacheFeature
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)

__all__ = ("MemoryCacheDriver", "memory_cache_registration")

_CAPABILITIES = CapabilitySet(feature.value for feature in CacheFeature)
_ARTIFACT_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(b"python:stdlib:memory-cache:v1").hexdigest()


@dataclass(frozen=True, slots=True)
class _Entry:
    value: bytes
    expires_at: datetime | None


class MemoryCacheDriver:
    """Serialized local cache with exact namespace isolation and lazy expiry."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("memory cache clock must be callable")
        self._entries: dict[tuple[CacheNamespace, CacheKey], _Entry] = {}
        self._lock = RLock()
        self._closed = False

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="cache.memory.clock",
            )
        return value.astimezone(UTC)

    def _assert_open(self) -> None:
        if self._closed:
            raise PlatformError(
                PlatformReason.SHUTDOWN_FAILED,
                stage="cache.memory.connection",
            )

    def get(self, namespace: CacheNamespace, key: CacheKey, /) -> bytes | None:
        namespace, key = _validated_operation(namespace, key)
        with self._lock:
            self._assert_open()
            entry = self._entries.get((namespace, key))
            if entry is None:
                return None
            if entry.expires_at is not None and entry.expires_at <= self._now():
                self._entries.pop((namespace, key), None)
                return None
            return entry.value

    def set(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        value: bytes,
        /,
        *,
        ttl: timedelta | None = None,
    ) -> None:
        namespace, key = _validated_operation(namespace, key)
        value = _validated_value(value)
        ttl = _validated_ttl(ttl)
        with self._lock:
            self._assert_open()
            expires_at = None if ttl is None else self._now() + ttl
            self._entries[(namespace, key)] = _Entry(value, expires_at)

    def delete(self, namespace: CacheNamespace, key: CacheKey, /) -> bool:
        namespace, key = _validated_operation(namespace, key)
        with self._lock:
            self._assert_open()
            return self._entries.pop((namespace, key), None) is not None

    def flush(self, namespace: CacheNamespace, /) -> int:
        if not isinstance(namespace, CacheNamespace):
            raise TypeError("cache flush requires a validated namespace")
        with self._lock:
            self._assert_open()
            keys = tuple(key for key in self._entries if key[0] == namespace)
            for key in keys:
                self._entries.pop(key, None)
            return len(keys)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._entries.clear()
            self._closed = True


def _evidence_digest(
    binding: CapabilityBinding,
    capabilities: CapabilitySet,
    observed_at: datetime,
) -> str:
    payload = {
        "artifact": binding.artifact_digest,
        "capabilities": tuple(capabilities),
        "configuration": binding.configuration_digest,
        "dependencies": binding.dependency_graph_digest,
        "observed_at": observed_at.isoformat(),
        "policy": binding.policy_version,
        "profile": binding.deployment_profile,
        "version": binding.driver_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def memory_cache_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[CacheDriver]:
    """Return an inert memory-cache declaration with runtime evidence."""

    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="memory",
        version="1",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> CacheDriver:
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "memory"
            or set(config) != {"driver"}
            or config.get("driver") != "memory"
        ):
            raise ValueError("memory cache connection configuration is invalid")
        return MemoryCacheDriver(clock=evidence_clock)

    def verify(
        ref: ConnectionRef,
        driver: CacheDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "memory"
            or type(driver) is not MemoryCacheDriver
        ):
            raise ValueError("memory cache runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("memory cache evidence clock must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        return CapabilityEvidence(
            binding=expected,
            capabilities=_CAPABILITIES,
            health=HealthStatus.HEALTHY,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=1),
            evidence_digest=_evidence_digest(expected, _CAPABILITIES, observed_at),
        )

    return DriverRegistration(
        manager_id="cache",
        driver_name="memory",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
