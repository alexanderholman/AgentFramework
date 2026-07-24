"""Verified MariaDB registration for DatabaseManager."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import platform
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
from af.infrastructure.mariadb import MariaDBDriver

__all__ = ("mariadb_registration",)

_CAPABILITIES = CapabilitySet(
    (
        DatabaseFeature.TRANSACTIONS.value,
        DatabaseFeature.SAVEPOINTS.value,
        DatabaseFeature.STREAMING.value,
        DatabaseFeature.CANCELLATION.value,
        DatabaseFeature.POOLING.value,
    )
)


def _runtime_identity() -> tuple[str, str, str]:
    spec = importlib.util.find_spec("pymysql")
    if spec is None or spec.origin is None:
        raise ValueError("MariaDB registration requires the pymysql runtime")
    try:
        pymysql_version = importlib.metadata.version("pymysql")
        sqlalchemy_version = importlib.metadata.version("SQLAlchemy")
        pymysql_bytes = Path(spec.origin).read_bytes()
        driver_bytes = Path(__file__).with_name("mariadb.py").read_bytes()
        shared_bytes = Path(__file__).with_name("mysql.py").read_bytes()
    except (OSError, importlib.metadata.PackageNotFoundError) as error:
        raise ValueError("MariaDB runtime identity is unavailable") from error
    artifact_digest = hashlib.sha256(
        driver_bytes + b"\0" + shared_bytes + b"\0" + pymysql_bytes
    ).hexdigest()
    dependency_digest = hashlib.sha256(
        (
            f"{platform.python_implementation()}:{platform.python_version()}:"
            f"sqlalchemy={sqlalchemy_version}:pymysql={pymysql_version}:"
            "dialect=mariadb+pymysql"
        ).encode()
    ).hexdigest()
    return pymysql_version, artifact_digest, dependency_digest


def _factory(ref: ConnectionRef, config: Mapping[str, object]) -> DatabaseDriver:
    if ref.manager_id != "database" or ref.driver_name != "mariadb":
        raise ValueError("MariaDB registration received an invalid connection identity")
    allowed = {"driver", "url", "pool_size", "pool_timeout"}
    if (
        set(config) - allowed
        or not {"driver", "url"} <= set(config)
        or config.get("driver") != "mariadb"
    ):
        raise ValueError("MariaDB connection configuration is invalid")
    url = config.get("url")
    if type(url) is not str or not url:
        raise ValueError("MariaDB connection URL is invalid")
    pooled = "pool_size" in config or "pool_timeout" in config
    if not pooled:
        return MariaDBDriver(url)
    pool_size = config.get("pool_size", 1)
    pool_timeout = config.get("pool_timeout", 5.0)
    if type(pool_size) is not int or not 1 <= pool_size <= 256:
        raise ValueError("MariaDB pool size is invalid")
    if (
        isinstance(pool_timeout, bool)
        or not isinstance(pool_timeout, (int, float))
        or not 0 < float(pool_timeout) <= 3_600.0
    ):
        raise ValueError("MariaDB pool timeout is invalid")
    return DatabasePool(
        lambda: MariaDBDriver(url),
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


def mariadb_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[DatabaseDriver]:
    """Return an inert MariaDB declaration with independent runtime evidence."""

    pymysql_version, artifact_digest, dependency_digest = _runtime_identity()
    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="mariadb",
        version=pymysql_version,
        artifact_digest=artifact_digest,
        dependency_graph_digest=dependency_digest,
        capabilities=_CAPABILITIES,
    )

    def verify(
        ref: ConnectionRef,
        driver: DatabaseDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "database"
            or ref.driver_name != "mariadb"
            or type(driver) not in {MariaDBDriver, DatabasePool}
        ):
            raise ValueError("MariaDB runtime capability verification failed")
        expected_features = {
            DatabaseFeature.TRANSACTIONS,
            DatabaseFeature.SAVEPOINTS,
            DatabaseFeature.STREAMING,
            DatabaseFeature.CANCELLATION,
        }
        if type(driver) is DatabasePool:
            expected_features.add(DatabaseFeature.POOLING)
        if driver.features != frozenset(expected_features):
            raise ValueError("MariaDB runtime capability verification failed")
        capabilities = CapabilitySet(
            feature.value for feature in sorted(driver.features, key=lambda item: item.value)
        )
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("MariaDB evidence clock must be timezone-aware")
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
        driver_name="mariadb",
        factory=_factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
