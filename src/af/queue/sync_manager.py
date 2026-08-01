"""Verified sync registration for QueueManager."""

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
from af.queue.contracts import QueueJob
from af.queue.manager import QueueDriver, QueueFeature
from af.queue.sync import SyncQueueDriver

__all__ = ("sync_queue_registration",)

_CAPABILITIES = CapabilitySet((QueueFeature.INLINE_EXECUTION.value,))
_ARTIFACT_DIGEST = hashlib.sha256(Path(__file__).with_name("sync.py").read_bytes()).hexdigest()
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


def sync_queue_registration(
    *,
    consumer: Callable[[QueueJob], None],
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[QueueDriver]:
    """Return an inert synchronous queue declaration with runtime evidence."""

    if not callable(consumer):
        raise TypeError("sync queue consumer must be callable")
    if clock is not None and not callable(clock):
        raise TypeError("sync queue clock must be callable")
    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="sync",
        version="1.0.0",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> QueueDriver:
        if ref.manager_id != "queue" or ref.driver_name != "sync":
            raise ValueError("sync queue registration received an invalid identity")
        if set(config) != {"driver"} or config.get("driver") != "sync":
            raise ValueError("sync queue connection configuration is invalid")
        return SyncQueueDriver(consumer, clock=evidence_clock)

    def verify(
        ref: ConnectionRef,
        driver: QueueDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "queue"
            or ref.driver_name != "sync"
            or type(driver) is not SyncQueueDriver
        ):
            raise ValueError("sync queue runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("sync queue evidence clock must be timezone-aware")
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
        manager_id="queue",
        driver_name="sync",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
