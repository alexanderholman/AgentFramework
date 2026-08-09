"""Verified relational database registration for QueueManager."""

from __future__ import annotations

import hashlib
import json
import platform
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
from af.framework.relational import DatabaseDriver
from af.queue.database import DatabaseQueueDriver
from af.queue.manager import QueueDriver, QueueFeature

__all__ = ("database_queue_registration",)

_CAPABILITIES = CapabilitySet(
    (
        QueueFeature.RESERVATION.value,
        QueueFeature.RESERVATION_RENEWAL.value,
        QueueFeature.RETRY.value,
    )
)
_ARTIFACT_DIGEST = hashlib.sha256(Path(__file__).with_name("database.py").read_bytes()).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(
    f"{platform.python_implementation()}:{platform.python_version()}".encode()
).hexdigest()


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


def database_queue_registration(
    *,
    database_factory: Callable[[], DatabaseDriver],
    clock: Callable[[], datetime] | None = None,
    reservation_lease: timedelta = timedelta(minutes=5),
) -> DriverRegistration[QueueDriver]:
    """Return an inert durable queue declaration backed by a relational driver."""

    if not callable(database_factory):
        raise TypeError("database queue factory must be callable")
    if clock is not None and not callable(clock):
        raise TypeError("database queue clock must be callable")
    if (
        not isinstance(reservation_lease, timedelta)
        or reservation_lease <= timedelta(0)
        or reservation_lease > timedelta(days=1)
    ):
        raise ValueError("database queue reservation lease must be within one day")
    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="database",
        version="1.3.0",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> QueueDriver:
        if ref.manager_id != "queue" or ref.driver_name != "database":
            raise ValueError("database queue registration received an invalid identity")
        if set(config) != {"driver"} or config.get("driver") != "database":
            raise ValueError("database queue connection configuration is invalid")
        return DatabaseQueueDriver(
            database_factory(),
            clock=evidence_clock,
            reservation_lease=reservation_lease,
        )

    def verify(
        ref: ConnectionRef,
        driver: QueueDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "queue"
            or ref.driver_name != "database"
            or type(driver) is not DatabaseQueueDriver
        ):
            raise ValueError("database queue runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("database queue evidence clock must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        if driver.features != frozenset(
            {
                QueueFeature.RESERVATION,
                QueueFeature.RESERVATION_RENEWAL,
                QueueFeature.RETRY,
            }
        ):
            raise ValueError("database queue runtime capability verification failed")
        return CapabilityEvidence(
            binding=expected,
            capabilities=_CAPABILITIES,
            health=HealthStatus.HEALTHY,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=1),
            evidence_digest=_evidence_digest(expected, _CAPABILITIES, observed_at),
        )

    return DriverRegistration(
        manager_id="queue",
        driver_name="database",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
