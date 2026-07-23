from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from af.framework.managers import (
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverTarget,
    FailoverDriver,
    HealthStatus,
    Manager,
)

_NOW = datetime(2026, 7, 23, 20, tzinfo=UTC)
_ARTIFACT_DIGEST = "a" * 64
_DEPENDENCY_DIGEST = "b" * 64
_EVIDENCE_DIGEST = "c" * 64


@dataclass(frozen=True, slots=True)
class _Driver:
    identity: int


def test_bare_capability_string_is_one_capability_during_manager_resolution() -> None:
    capabilities = CapabilitySet("transactions")
    assert tuple(capabilities) == ("transactions",)

    driver = _Driver(1)
    manager: Manager[_Driver] = Manager(
        manager_id="database",
        connections={"primary": {"driver": "remote"}},
        default="primary",
        deployment_profile="production",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.extend(
        "remote",
        lambda _ref, _config: driver,
        manifest=DriverManifest(
            driver_name="remote",
            version="1.0.0",
            artifact_digest=_ARTIFACT_DIGEST,
            dependency_graph_digest=_DEPENDENCY_DIGEST,
            capabilities=capabilities,
        ),
        verifier=lambda _ref, _driver, binding: CapabilityEvidence(
            binding=binding,
            capabilities=capabilities,
            health=HealthStatus.HEALTHY,
            observed_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
            evidence_digest=_EVIDENCE_DIGEST,
        ),
    )

    assert manager.connection(requires="transactions") is driver


def test_capability_string_subclass_is_rejected_instead_of_split() -> None:
    class _Capability(str):
        pass

    with pytest.raises(ValueError):
        CapabilitySet(_Capability("transactions"))


@pytest.mark.parametrize(
    ("reason_code", "clock"),
    [
        ("INVALID reason", lambda: _NOW),
        ("health_probe_failed", lambda: datetime(2026, 7, 23, 20)),
    ],
    ids=("invalid-reason", "invalid-clock"),
)
def test_invalid_failover_transition_preserves_target_and_evidence(
    reason_code: str,
    clock: Callable[[], datetime],
) -> None:
    primary = DriverTarget(
        ConnectionRef("database", "primary", "remote"),
        _Driver(1),
    )
    secondary = DriverTarget(
        ConnectionRef("database", "secondary", "remote"),
        _Driver(2),
    )
    failover = FailoverDriver((primary, secondary), clock=clock)
    original_target = failover.current
    original_evidence = failover.evidence

    with pytest.raises(ValueError):
        failover.failover(reason_code)

    assert failover.current is original_target
    assert failover.evidence == original_evidence
