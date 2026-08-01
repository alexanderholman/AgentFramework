"""Verified null cache driver for explicit no-storage configurations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

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

__all__ = ("NullCacheDriver", "null_cache_registration")

_CAPABILITIES = CapabilitySet(feature.value for feature in CacheFeature)
_ARTIFACT_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(b"python:stdlib:null-cache:v1").hexdigest()


class NullCacheDriver:
    """Validate cache operations while deliberately retaining no values."""

    def __init__(self) -> None:
        self._closed = False

    def _assert_open(self) -> None:
        if self._closed:
            raise PlatformError(
                PlatformReason.SHUTDOWN_FAILED,
                stage="cache.null.connection",
            )

    def get(self, namespace: CacheNamespace, key: CacheKey, /) -> bytes | None:
        _validated_operation(namespace, key)
        self._assert_open()
        return None

    def set(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        value: bytes,
        /,
        *,
        ttl: timedelta | None = None,
    ) -> None:
        _validated_operation(namespace, key)
        _validated_value(value)
        _validated_ttl(ttl)
        self._assert_open()

    def delete(self, namespace: CacheNamespace, key: CacheKey, /) -> bool:
        _validated_operation(namespace, key)
        self._assert_open()
        return False

    def flush(self, namespace: CacheNamespace, /) -> int:
        if not isinstance(namespace, CacheNamespace):
            raise TypeError("cache flush requires a validated namespace")
        self._assert_open()
        return 0

    def close(self) -> None:
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


def null_cache_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[CacheDriver]:
    """Return an inert null-cache declaration with runtime evidence."""

    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="null",
        version="1",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> CacheDriver:
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "null"
            or set(config) != {"driver"}
            or config.get("driver") != "null"
        ):
            raise ValueError("null cache connection configuration is invalid")
        return NullCacheDriver()

    def verify(
        ref: ConnectionRef,
        driver: CacheDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "null"
            or type(driver) is not NullCacheDriver
        ):
            raise ValueError("null cache runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("null cache evidence clock must be timezone-aware")
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
        driver_name="null",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
