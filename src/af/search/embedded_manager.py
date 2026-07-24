"""Verified embedded SQLite registration for SearchManager."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)
from af.search.embedded import EmbeddedSearchDriver
from af.search.manager import SearchDriver, SearchFeature

__all__ = ("embedded_search_registration",)

_CAPABILITIES = CapabilitySet(feature.value for feature in SearchFeature)
_ARTIFACT_DIGEST = hashlib.sha256(
    Path(__file__).with_name("embedded.py").read_bytes()
    + b"\0"
    + Path(sqlite3.__file__).read_bytes()
).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(
    (
        f"{platform.python_implementation()}:{platform.python_version()}:"
        f"{sqlite3.sqlite_version}:fts5"
    ).encode()
).hexdigest()


def _factory(ref: ConnectionRef, config: Mapping[str, object]) -> SearchDriver:
    if ref.manager_id != "search" or ref.driver_name != "embedded":
        raise ValueError("embedded search registration received an invalid identity")
    if (
        set(config) != {"driver", "database"}
        or config.get("driver") != "embedded"
        or type(config.get("database")) is not str
        or not config.get("database")
    ):
        raise ValueError("embedded search connection configuration is invalid")
    return EmbeddedSearchDriver(config["database"])  # type: ignore[arg-type]


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


def embedded_search_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[SearchDriver]:
    """Return an inert embedded-search declaration with runtime evidence."""

    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="embedded",
        version=sqlite3.sqlite_version,
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def verify(
        ref: ConnectionRef,
        driver: SearchDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "search"
            or ref.driver_name != "embedded"
            or type(driver) is not EmbeddedSearchDriver
        ):
            raise ValueError("embedded search runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("embedded search evidence clock must be timezone-aware")
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
        manager_id="search",
        driver_name="embedded",
        factory=_factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
