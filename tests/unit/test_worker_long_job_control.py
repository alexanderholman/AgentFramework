from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from af.queue import (
    QueueJobTags,
    WorkerCheckpoint,
    WorkerEtaRange,
    WorkerJobCoordinates,
    WorkerObservation,
    WorkerObservationRegistry,
    WorkerProgressSnapshot,
    WorkerResourceSnapshot,
)

NOW = datetime(2026, 8, 19, 12, tzinfo=UTC)


def test_long_job_coordinates_are_complete_immutable_and_queue_tagged() -> None:
    coordinates = WorkerJobCoordinates(
        workflow_id="workflow.acquire",
        run_id="run.20260819",
        node_id="node.verify",
        checkpoint_id="checkpoint.4",
    )
    tags = QueueJobTags(coordinates=coordinates)

    assert tags.coordinates is coordinates
    with pytest.raises(FrozenInstanceError):
        coordinates.node_id = "node.changed"  # type: ignore[misc]


def test_progress_represents_units_bytes_active_work_and_stable_blocking() -> None:
    progress = WorkerProgressSnapshot(
        completed_units=4,
        total_units=10,
        completed_bytes=1024,
        total_bytes=4096,
        active_unit="artifact.mobileclip",
        updated_at=NOW,
        blocked_reason="predecessor.evidence_missing",
    )

    assert progress.fraction == pytest.approx(0.4)
    assert progress.blocked

    with pytest.raises(ValueError, match="completed units"):
        WorkerProgressSnapshot(completed_units=11, total_units=10, updated_at=NOW)
    with pytest.raises(ValueError, match="stable reason"):
        WorkerProgressSnapshot(
            completed_units=0,
            total_units=1,
            updated_at=NOW,
            blocked_reason="Waiting for someone's secret payload",
        )


def test_eta_is_a_fresh_range_with_evidence_and_unknown_is_none() -> None:
    eta = WorkerEtaRange(
        lower=timedelta(minutes=4),
        upper=timedelta(minutes=7),
        observed_throughput=2.5,
        throughput_unit="artifacts_per_minute",
        assumptions=("stable.network", "verified.inputs"),
        confidence=0.7,
        observed_at=NOW,
        fresh_until=NOW + timedelta(minutes=1),
    )

    assert eta.is_stale(NOW + timedelta(minutes=2))
    assert eta.lower < eta.upper

    with pytest.raises(ValueError, match="positive"):
        WorkerEtaRange(
            lower=timedelta(0),
            upper=timedelta(minutes=1),
            observed_throughput=1.0,
            throughput_unit="units_per_second",
            assumptions=(),
            confidence=0.5,
            observed_at=NOW,
            fresh_until=NOW + timedelta(seconds=1),
        )


def test_checkpoint_resume_requires_exact_compatibility_and_verified_artifact() -> None:
    checkpoint = WorkerCheckpoint(
        checkpoint_id="checkpoint.4",
        schema_version=2,
        runtime_id="runtime.cuda12",
        artifact_digest=f"sha256:{'a' * 64}",
    )

    assert checkpoint.compatible_with(
        schema_version=2,
        runtime_id="runtime.cuda12",
        artifact_digest=f"sha256:{'a' * 64}",
    )
    assert not checkpoint.compatible_with(
        schema_version=2,
        runtime_id="runtime.cuda12",
        artifact_digest=f"sha256:{'b' * 64}",
    )


def test_resource_snapshot_covers_bounded_worker_capacity_and_delivery_cost() -> None:
    snapshot = WorkerResourceSnapshot(
        cpu_percent=50.0,
        ram_bytes=4_000_000,
        vram_bytes=8_000_000,
        storage_bytes=12_000_000,
        queue_wait=timedelta(seconds=2),
        runtime=timedelta(seconds=7),
        retry=1,
        saturation=0.75,
    )

    assert snapshot.telemetry_attributes() == {
        "cpu_bucket": "lt_80pct",
        "queue_wait_bucket": "lt_10s",
        "ram_bucket": "lt_1gib",
        "retry_bucket": "retry",
        "runtime_bucket": "lt_10s",
        "saturation_bucket": "lt_80pct",
        "storage_bucket": "lt_1gib",
        "vram_bucket": "lt_1gib",
    }

    with pytest.raises(ValueError, match="saturation"):
        WorkerResourceSnapshot(saturation=1.01)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("total_units", -1, "total units"),
        ("completed_bytes", 1, "byte progress"),
        ("total_bytes", 1, "byte progress"),
    ],
)
def test_progress_rejects_incomplete_or_negative_evidence(
    field: str,
    value: object,
    message: str,
) -> None:
    fields: dict[str, object] = {
        "completed_units": 0,
        "total_units": 1,
        "updated_at": NOW,
    }
    fields[field] = value
    with pytest.raises(ValueError, match=message):
        WorkerProgressSnapshot(**fields)  # type: ignore[arg-type]


def test_progress_preserves_unknown_total_and_eta() -> None:
    progress = WorkerProgressSnapshot(completed_units=3, total_units=None, updated_at=NOW)

    assert progress.fraction is None
    assert progress.eta is None
    assert not progress.blocked


@pytest.mark.parametrize(
    "changes",
    [
        {"upper": timedelta(seconds=1), "lower": timedelta(seconds=2)},
        {"observed_throughput": float("inf")},
        {"confidence": 1.1},
        {"fresh_until": NOW},
        {"assumptions": tuple(f"assumption.{index}" for index in range(9))},
    ],
)
def test_eta_rejects_unbounded_or_unsupported_evidence(changes: dict[str, object]) -> None:
    fields: dict[str, object] = {
        "lower": timedelta(seconds=1),
        "upper": timedelta(seconds=2),
        "observed_throughput": 1.0,
        "throughput_unit": "units_per_second",
        "assumptions": (),
        "confidence": 0.5,
        "observed_at": NOW,
        "fresh_until": NOW + timedelta(seconds=1),
    }
    fields.update(changes)
    with pytest.raises(ValueError):
        WorkerEtaRange(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"cpu_percent": -1},
        {"ram_bytes": -1},
        {"queue_wait": timedelta(seconds=-1)},
        {"retry": -1},
    ],
)
def test_resource_snapshot_rejects_invalid_measurements(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        WorkerResourceSnapshot(**changes)  # type: ignore[arg-type]


def test_resource_snapshot_uses_fixed_buckets_at_large_values() -> None:
    snapshot = WorkerResourceSnapshot(
        cpu_percent=100,
        ram_bytes=80 * 1024**3,
        queue_wait=timedelta(minutes=2),
        runtime=timedelta(milliseconds=500),
        saturation=1,
    )

    assert snapshot.telemetry_attributes() == {
        "cpu_bucket": "full",
        "queue_wait_bucket": "gte_1m",
        "ram_bucket": "gte_64gib",
        "retry_bucket": "first",
        "runtime_bucket": "lt_1s",
        "saturation_bucket": "full",
    }


def _coordinates(run_id: str) -> WorkerJobCoordinates:
    return WorkerJobCoordinates(
        workflow_id="workflow.acquire",
        run_id=run_id,
        node_id="node.verify",
        checkpoint_id="checkpoint.4",
    )


def _observation(run_id: str, completed: int) -> WorkerObservation:
    return WorkerObservation(
        coordinates=_coordinates(run_id),
        progress=WorkerProgressSnapshot(
            completed_units=completed,
            total_units=10,
            updated_at=NOW + timedelta(seconds=completed),
        ),
        resources=WorkerResourceSnapshot(runtime=timedelta(seconds=completed)),
    )


def test_observation_registry_isolates_runs_with_the_same_workflow_node() -> None:
    registry = WorkerObservationRegistry(capacity=2)
    first = _observation("run.first", 1)
    second = _observation("run.second", 2)

    registry.record(first)
    registry.record(second)

    assert registry.get(first.coordinates) is first
    assert registry.get(second.coordinates) is second
    assert registry.snapshots() == (first, second)


def test_observation_registry_rejects_progress_and_time_regressions() -> None:
    registry = WorkerObservationRegistry()
    registry.record(_observation("run.monotonic", 4))

    with pytest.raises(ValueError, match="regress"):
        registry.record(_observation("run.monotonic", 3))


def test_observation_registry_fails_closed_at_capacity_until_forgotten() -> None:
    registry = WorkerObservationRegistry(capacity=1)
    first = _observation("run.first", 1)
    second = _observation("run.second", 1)
    registry.record(first)

    with pytest.raises(RuntimeError, match="capacity"):
        registry.record(second)

    assert registry.forget(first.coordinates) is first
    registry.record(second)
    assert registry.get(second.coordinates) is second


def test_observation_registry_rejects_changed_totals_for_one_run() -> None:
    registry = WorkerObservationRegistry()
    original = _observation("run.stable-total", 1)
    registry.record(original)
    changed = WorkerObservation(
        coordinates=original.coordinates,
        progress=WorkerProgressSnapshot(
            completed_units=2,
            total_units=11,
            updated_at=NOW + timedelta(seconds=2),
        ),
    )

    with pytest.raises(ValueError, match="total units"):
        registry.record(changed)
