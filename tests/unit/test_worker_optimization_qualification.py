from __future__ import annotations

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.application.worker_generation import WorkerRuntimeOptimization
from af.application.worker_qualification import WorkerOptimizationQualification
from af.framework.errors import InvalidProviderConfiguration
from af.framework.managers import CapabilityBinding
from af.infrastructure import worker_optimization as profile_module
from af.infrastructure.worker_optimization import (
    load_worker_optimization_profile,
    record_worker_optimization_profile,
)

NOW = datetime(2026, 8, 18, 9, 30, tzinfo=UTC)


def _binding() -> CapabilityBinding:
    return CapabilityBinding(
        driver_version="1.5.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest="c" * 64,
        deployment_profile="production",
        policy_version="worker-policy-v1",
    )


def _qualification(**overrides: object) -> WorkerOptimizationQualification:
    values: dict[str, object] = {
        "driver_name": "database",
        "binding": _binding(),
        "optimization": WorkerRuntimeOptimization(
            worker_count=3,
            max_concurrency=5,
            poll_interval=timedelta(milliseconds=25),
            heartbeat_interval=timedelta(seconds=5),
            max_idle_time=timedelta(minutes=2),
            queue_wait_alert_after=timedelta(seconds=3),
            renewal_min_interval=timedelta(milliseconds=100),
            renewal_max_interval=timedelta(seconds=10),
        ),
        "evidence_digest": "d" * 64,
        "environment_digest": "e" * 64,
        "observed_at": NOW - timedelta(hours=1),
        "expires_at": NOW + timedelta(days=7),
        "sample_count": 10_000,
        "real_service": True,
        "correctness_failures": 0,
        "settlement_failures": 0,
        "reservation_losses": 0,
    }
    values.update(overrides)
    return WorkerOptimizationQualification(**values)  # type: ignore[arg-type]


def test_recorder_emits_canonical_exact_binding_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "worker-profile.json"
    qualification = _qualification()

    profile_digest = record_worker_optimization_profile(
        profile_path,
        qualification,
        now=NOW,
    )

    document = json.loads(profile_path.read_text(encoding="utf-8"))
    assert document["schema_version"] == 2
    assert document["qualification"] == {
        "correctness_failures": 0,
        "environment_digest": "e" * 64,
        "evidence_digest": "d" * 64,
        "expires_at": "2026-08-25T09:30:00.000000Z",
        "observed_at": "2026-08-18T08:30:00.000000Z",
        "real_service": True,
        "reservation_losses": 0,
        "sample_count": 10_000,
        "settlement_failures": 0,
    }
    assert profile_path.read_bytes().endswith(b"\n")
    assert stat.S_IMODE(profile_path.stat().st_mode) == 0o600

    provider = load_worker_optimization_profile(profile_path, now=NOW)
    assert provider.binding == qualification.binding
    assert provider.driver_name == "database"
    assert provider.profile_digest == profile_digest
    assert provider.qualification_digest == qualification.evidence_digest


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"real_service": False}, "real-service"),
        ({"correctness_failures": 1}, "correctness"),
        ({"settlement_failures": 1}, "settlement"),
        ({"reservation_losses": 1}, "reservation"),
        ({"observed_at": NOW + timedelta(seconds=1)}, "future"),
        ({"expires_at": NOW}, "expired"),
    ],
)
def test_recorder_rejects_unqualified_evidence_without_replacing_profile(
    tmp_path: Path,
    overrides: dict[str, object],
    message: str,
) -> None:
    profile_path = tmp_path / "worker-profile.json"
    profile_path.write_bytes(b"existing-profile")

    with pytest.raises(InvalidProviderConfiguration, match=message):
        record_worker_optimization_profile(
            profile_path,
            _qualification(**overrides),
            now=NOW,
        )

    assert profile_path.read_bytes() == b"existing-profile"


def test_recorder_preserves_existing_profile_when_atomic_promotion_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "worker-profile.json"
    profile_path.write_bytes(b"existing-profile")

    def reject_promotion(source: object, target: object) -> None:
        raise OSError("simulated promotion failure")

    monkeypatch.setattr(profile_module.os, "replace", reject_promotion)

    with pytest.raises(InvalidProviderConfiguration, match="could not be recorded"):
        record_worker_optimization_profile(profile_path, _qualification(), now=NOW)

    assert profile_path.read_bytes() == b"existing-profile"
    assert list(tmp_path.iterdir()) == [profile_path]


def test_recorder_rejects_lossy_duration_conversion(tmp_path: Path) -> None:
    profile_path = tmp_path / "worker-profile.json"
    optimization = replace(
        _qualification().optimization,
        poll_interval=timedelta(microseconds=1_001),
    )

    with pytest.raises(InvalidProviderConfiguration, match="could not be recorded"):
        record_worker_optimization_profile(
            profile_path,
            _qualification(optimization=optimization),
            now=NOW,
        )

    assert not profile_path.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("real_service", False),
        ("correctness_failures", 1),
        ("settlement_failures", 1),
        ("reservation_losses", 1),
    ],
)
def test_loader_revalidates_embedded_safety_evidence(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    profile_path = tmp_path / "worker-profile.json"
    record_worker_optimization_profile(profile_path, _qualification(), now=NOW)
    document = json.loads(profile_path.read_text(encoding="utf-8"))
    document["qualification"][field] = value
    profile_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(InvalidProviderConfiguration):
        load_worker_optimization_profile(profile_path, now=NOW)


def test_loader_rejects_legacy_or_stale_unqualified_profile(tmp_path: Path) -> None:
    profile_path = tmp_path / "worker-profile.json"
    qualification = _qualification()
    record_worker_optimization_profile(profile_path, qualification, now=NOW)
    document = json.loads(profile_path.read_text(encoding="utf-8"))

    legacy = dict(document)
    legacy["schema_version"] = 1
    legacy.pop("qualification")
    profile_path.write_text(json.dumps(legacy), encoding="utf-8")
    with pytest.raises(InvalidProviderConfiguration):
        load_worker_optimization_profile(profile_path, now=NOW)

    stale = replace(qualification, expires_at=NOW + timedelta(seconds=1))
    record_worker_optimization_profile(profile_path, stale, now=NOW)
    with pytest.raises(InvalidProviderConfiguration):
        load_worker_optimization_profile(profile_path, now=NOW + timedelta(seconds=2))
