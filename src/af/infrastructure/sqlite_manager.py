"""Verified SQLite registration for DatabaseManager."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from af.framework.database_pool import DatabasePool
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)
from af.framework.relational import DatabaseDriver, DatabaseFeature
from af.infrastructure.sqlite import SQLiteDriver

__all__ = ("sqlite_registration",)

_CAPABILITIES = CapabilitySet(
    (
        DatabaseFeature.TRANSACTIONS.value,
        DatabaseFeature.TRANSACTIONAL_DDL.value,
        DatabaseFeature.SAVEPOINTS.value,
        DatabaseFeature.STREAMING.value,
        DatabaseFeature.CANCELLATION.value,
        DatabaseFeature.POOLING.value,
    )
)
_ARTIFACT_DIGEST = hashlib.sha256(
    Path(__file__).with_name("sqlite.py").read_bytes() + b"\0" + Path(sqlite3.__file__).read_bytes()
).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(
    (
        f"{platform.python_implementation()}:{platform.python_version()}:{sqlite3.sqlite_version}"
    ).encode()
).hexdigest()


def _factory(ref: ConnectionRef, config: Mapping[str, object]) -> DatabaseDriver:
    if ref.manager_id != "database" or ref.driver_name != "sqlite":
        raise ValueError("SQLite registration received an invalid connection identity")
    allowed = {"driver", "database", "pool_size", "pool_timeout"}
    if (
        set(config) - allowed
        or not {"driver", "database"} <= set(config)
        or config.get("driver") != "sqlite"
    ):
        raise ValueError("SQLite connection configuration is invalid")
    database = config.get("database")
    if type(database) is not str or not database:
        raise ValueError("SQLite database path is invalid")
    pooled = "pool_size" in config or "pool_timeout" in config
    if not pooled:
        return SQLiteDriver(database)
    pool_size = config.get("pool_size", 1)
    pool_timeout = config.get("pool_timeout", 5.0)
    if type(pool_size) is not int or not 1 <= pool_size <= 256:
        raise ValueError("SQLite pool size is invalid")
    if (
        isinstance(pool_timeout, bool)
        or not isinstance(pool_timeout, (int, float))
        or not 0 < float(pool_timeout) <= 3_600.0
    ):
        raise ValueError("SQLite pool timeout is invalid")
    if database == ":memory:" and pool_size > 1:
        raise ValueError("multi-connection SQLite pools require a persistent database")
    return DatabasePool(
        lambda: SQLiteDriver(database),
        size=pool_size,
        acquire_timeout=float(pool_timeout),
    )


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


def sqlite_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[DatabaseDriver]:
    """Return an inert SQLite declaration with independent runtime evidence."""

    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="sqlite",
        version=sqlite3.sqlite_version,
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def verify(
        ref: ConnectionRef,
        driver: DatabaseDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "database"
            or ref.driver_name != "sqlite"
            or type(driver) not in {SQLiteDriver, DatabasePool}
        ):
            raise ValueError("SQLite runtime capability verification failed")
        expected_features = {
            DatabaseFeature.TRANSACTIONS,
            DatabaseFeature.TRANSACTIONAL_DDL,
            DatabaseFeature.SAVEPOINTS,
            DatabaseFeature.STREAMING,
            DatabaseFeature.CANCELLATION,
        }
        if type(driver) is DatabasePool:
            expected_features.add(DatabaseFeature.POOLING)
        if driver.features != frozenset(expected_features):
            raise ValueError("SQLite runtime capability verification failed")
        capabilities = CapabilitySet(
            feature.value for feature in sorted(driver.features, key=lambda item: item.value)
        )
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("SQLite evidence clock must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        return CapabilityEvidence(
            binding=expected,
            capabilities=capabilities,
            health=HealthStatus.HEALTHY,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=1),
            evidence_digest=_evidence_digest(expected, capabilities, observed_at),
        )

    return DriverRegistration(
        manager_id="database",
        driver_name="sqlite",
        factory=_factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
