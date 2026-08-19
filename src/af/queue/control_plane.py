"""Immutable, driver-neutral control-plane evidence for long-running queue jobs."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from threading import RLock

__all__ = (
    "WorkerCheckpoint",
    "WorkerEtaRange",
    "WorkerJobCoordinates",
    "WorkerObservation",
    "WorkerObservationRegistry",
    "WorkerProgressSnapshot",
    "WorkerResourceSnapshot",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"worker {field} must be a canonical identifier")
    return value


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"worker {field} must be timezone-aware")
    return value.astimezone(UTC)


def _non_negative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"worker {field} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class WorkerJobCoordinates:
    """Complete workflow identity carried with a long-running queue job."""

    workflow_id: str
    run_id: str
    node_id: str
    checkpoint_id: str

    def __post_init__(self) -> None:
        for value, field in (
            (self.workflow_id, "workflow identifier"),
            (self.run_id, "run identifier"),
            (self.node_id, "node identifier"),
            (self.checkpoint_id, "checkpoint identifier"),
        ):
            _identifier(value, field)


@dataclass(frozen=True, slots=True)
class WorkerEtaRange:
    """Evidence-backed ETA interval; absence, rather than zero, means unknown."""

    lower: timedelta
    upper: timedelta
    observed_throughput: float
    throughput_unit: str
    assumptions: tuple[str, ...]
    confidence: float
    observed_at: datetime
    fresh_until: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.lower, timedelta) or self.lower <= timedelta(0):
            raise ValueError("worker ETA lower bound must be positive")
        if not isinstance(self.upper, timedelta) or self.upper < self.lower:
            raise ValueError("worker ETA upper bound must not precede its lower bound")
        if (
            isinstance(self.observed_throughput, bool)
            or not isinstance(self.observed_throughput, (int, float))
            or not isfinite(float(self.observed_throughput))
            or self.observed_throughput <= 0
        ):
            raise ValueError("worker ETA observed throughput must be finite and positive")
        _identifier(self.throughput_unit, "ETA throughput unit")
        if type(self.assumptions) is not tuple or len(self.assumptions) > 8:
            raise ValueError("worker ETA assumptions must be a bounded tuple")
        for assumption in self.assumptions:
            _identifier(assumption, "ETA assumption")
        if (
            isinstance(self.confidence, bool)
            or not isinstance(self.confidence, (int, float))
            or not isfinite(float(self.confidence))
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("worker ETA confidence must be between zero and one")
        observed_at = _aware(self.observed_at, "ETA observation time")
        fresh_until = _aware(self.fresh_until, "ETA freshness time")
        if fresh_until <= observed_at:
            raise ValueError("worker ETA freshness must follow its observation")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "fresh_until", fresh_until)
        object.__setattr__(self, "observed_throughput", float(self.observed_throughput))
        object.__setattr__(self, "confidence", float(self.confidence))

    def is_stale(self, at: datetime) -> bool:
        return _aware(at, "ETA comparison time") > self.fresh_until


@dataclass(frozen=True, slots=True)
class WorkerProgressSnapshot:
    """One coherent progress observation without inventing unknown totals or ETAs."""

    completed_units: int
    total_units: int | None
    updated_at: datetime
    completed_bytes: int | None = None
    total_bytes: int | None = None
    active_unit: str | None = None
    blocked_reason: str | None = None
    eta: WorkerEtaRange | None = None

    def __post_init__(self) -> None:
        completed_units = _non_negative_int(self.completed_units, "completed units")
        if self.total_units is not None:
            total_units = _non_negative_int(self.total_units, "total units")
            if completed_units > total_units:
                raise ValueError("worker completed units cannot exceed total units")
        if (self.completed_bytes is None) != (self.total_bytes is None):
            raise ValueError("worker byte progress must provide completed and total bytes")
        if self.completed_bytes is not None and self.total_bytes is not None:
            completed_bytes = _non_negative_int(self.completed_bytes, "completed bytes")
            total_bytes = _non_negative_int(self.total_bytes, "total bytes")
            if completed_bytes > total_bytes:
                raise ValueError("worker completed bytes cannot exceed total bytes")
        if self.active_unit is not None:
            _identifier(self.active_unit, "active unit")
        if self.blocked_reason is not None:
            try:
                _identifier(self.blocked_reason, "blocked stable reason")
            except ValueError as error:
                raise ValueError("worker blocked reason must be a stable reason code") from error
        if self.eta is not None and not isinstance(self.eta, WorkerEtaRange):
            raise TypeError("worker progress ETA is invalid")
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "progress update time"))

    @property
    def fraction(self) -> float | None:
        total_units = self.total_units
        if total_units is None or total_units == 0:
            return None
        return self.completed_units / total_units

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None


@dataclass(frozen=True, slots=True)
class WorkerCheckpoint:
    """Checkpoint compatibility evidence; #49 remains owner of durable DAG state."""

    checkpoint_id: str
    schema_version: int
    runtime_id: str
    artifact_digest: str

    def __post_init__(self) -> None:
        _identifier(self.checkpoint_id, "checkpoint identifier")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("worker checkpoint schema version must be positive")
        _identifier(self.runtime_id, "checkpoint runtime identifier")
        if type(self.artifact_digest) is not str or _DIGEST.fullmatch(self.artifact_digest) is None:
            raise ValueError("worker checkpoint artifact digest must be sha256")

    def compatible_with(
        self,
        *,
        schema_version: int,
        runtime_id: str,
        artifact_digest: str,
    ) -> bool:
        return (
            type(schema_version) is int
            and schema_version == self.schema_version
            and runtime_id == self.runtime_id
            and artifact_digest == self.artifact_digest
        )


def _duration_bucket(value: timedelta) -> str:
    seconds = value.total_seconds()
    if seconds < 1:
        return "lt_1s"
    if seconds < 10:
        return "lt_10s"
    if seconds < 60:
        return "lt_1m"
    return "gte_1m"


def _bytes_bucket(value: int) -> str:
    if value < 1024**3:
        return "lt_1gib"
    if value < 8 * 1024**3:
        return "lt_8gib"
    if value < 64 * 1024**3:
        return "lt_64gib"
    return "gte_64gib"


def _percentage_bucket(value: float) -> str:
    if value < 0.5:
        return "lt_50pct"
    if value < 0.8:
        return "lt_80pct"
    if value < 1:
        return "lt_100pct"
    return "full"


@dataclass(frozen=True, slots=True)
class WorkerResourceSnapshot:
    """Resource and delivery-cost observation projected to bounded telemetry labels."""

    cpu_percent: float | None = None
    ram_bytes: int | None = None
    vram_bytes: int | None = None
    storage_bytes: int | None = None
    queue_wait: timedelta | None = None
    runtime: timedelta | None = None
    retry: int = 0
    saturation: float = 0.0

    def __post_init__(self) -> None:
        if self.cpu_percent is not None and (
            isinstance(self.cpu_percent, bool)
            or not isinstance(self.cpu_percent, (int, float))
            or not isfinite(float(self.cpu_percent))
            or not 0 <= self.cpu_percent <= 100
        ):
            raise ValueError("worker CPU percentage must be between zero and one hundred")
        for byte_value, field in (
            (self.ram_bytes, "RAM bytes"),
            (self.vram_bytes, "VRAM bytes"),
            (self.storage_bytes, "storage bytes"),
        ):
            if byte_value is not None:
                _non_negative_int(byte_value, field)
        for duration, field in (
            (self.queue_wait, "queue wait"),
            (self.runtime, "runtime"),
        ):
            if duration is not None and (
                not isinstance(duration, timedelta) or duration < timedelta(0)
            ):
                raise ValueError(f"worker {field} must be a non-negative duration")
        _non_negative_int(self.retry, "retry")
        if (
            isinstance(self.saturation, bool)
            or not isinstance(self.saturation, (int, float))
            or not isfinite(float(self.saturation))
            or not 0 <= self.saturation <= 1
        ):
            raise ValueError("worker saturation must be between zero and one")

    def telemetry_attributes(self) -> dict[str, str]:
        attributes = {
            "retry_bucket": "first" if self.retry == 0 else "retry",
            "saturation_bucket": _percentage_bucket(float(self.saturation)),
        }
        if self.cpu_percent is not None:
            attributes["cpu_bucket"] = _percentage_bucket(float(self.cpu_percent) / 100)
        for value, key in (
            (self.ram_bytes, "ram_bucket"),
            (self.vram_bytes, "vram_bucket"),
            (self.storage_bytes, "storage_bucket"),
        ):
            if value is not None:
                attributes[key] = _bytes_bucket(value)
        if self.queue_wait is not None:
            attributes["queue_wait_bucket"] = _duration_bucket(self.queue_wait)
        if self.runtime is not None:
            attributes["runtime_bucket"] = _duration_bucket(self.runtime)
        return dict(sorted(attributes.items()))


@dataclass(frozen=True, slots=True)
class WorkerObservation:
    """One isolated control-plane observation for an exact workflow run and node."""

    coordinates: WorkerJobCoordinates
    progress: WorkerProgressSnapshot
    resources: WorkerResourceSnapshot | None = None
    checkpoint: WorkerCheckpoint | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.coordinates, WorkerJobCoordinates):
            raise TypeError("worker observation coordinates are invalid")
        if not isinstance(self.progress, WorkerProgressSnapshot):
            raise TypeError("worker observation progress is invalid")
        if self.resources is not None and not isinstance(self.resources, WorkerResourceSnapshot):
            raise TypeError("worker observation resources are invalid")
        if self.checkpoint is not None:
            if not isinstance(self.checkpoint, WorkerCheckpoint):
                raise TypeError("worker observation checkpoint is invalid")
            if self.checkpoint.checkpoint_id != self.coordinates.checkpoint_id:
                raise ValueError("worker observation checkpoint identity diverges")


class WorkerObservationRegistry:
    """Bounded thread-safe current observations with monotonic per-run updates."""

    def __init__(self, *, capacity: int = 1_024) -> None:
        if type(capacity) is not int or capacity < 1:
            raise ValueError("worker observation capacity must be positive")
        self._capacity = capacity
        self._observations: OrderedDict[WorkerJobCoordinates, WorkerObservation] = OrderedDict()
        self._lock = RLock()

    def record(self, observation: WorkerObservation, /) -> WorkerObservation:
        if not isinstance(observation, WorkerObservation):
            raise TypeError("worker observation registry requires a WorkerObservation")
        coordinates = observation.coordinates
        with self._lock:
            current = self._observations.get(coordinates)
            if current is None:
                if len(self._observations) >= self._capacity:
                    raise RuntimeError("worker observation registry capacity is exhausted")
                self._observations[coordinates] = observation
                return observation
            self._validate_transition(current, observation)
            if current == observation:
                return current
            self._observations[coordinates] = observation
            return observation

    def get(self, coordinates: WorkerJobCoordinates, /) -> WorkerObservation | None:
        if not isinstance(coordinates, WorkerJobCoordinates):
            raise TypeError("worker observation lookup coordinates are invalid")
        with self._lock:
            return self._observations.get(coordinates)

    def forget(self, coordinates: WorkerJobCoordinates, /) -> WorkerObservation | None:
        if not isinstance(coordinates, WorkerJobCoordinates):
            raise TypeError("worker observation forget coordinates are invalid")
        with self._lock:
            return self._observations.pop(coordinates, None)

    def snapshots(self) -> tuple[WorkerObservation, ...]:
        with self._lock:
            return tuple(self._observations.values())

    @staticmethod
    def _validate_transition(current: WorkerObservation, updated: WorkerObservation) -> None:
        previous = current.progress
        next_progress = updated.progress
        if next_progress.updated_at < previous.updated_at:
            raise ValueError("worker observation update time cannot regress")
        if next_progress.completed_units < previous.completed_units:
            raise ValueError("worker observation completed units cannot regress")
        if next_progress.total_units != previous.total_units:
            raise ValueError("worker observation total units cannot change")
        if next_progress.total_bytes != previous.total_bytes:
            raise ValueError("worker observation total bytes cannot change")
        if (
            previous.completed_bytes is not None
            and next_progress.completed_bytes is not None
            and next_progress.completed_bytes < previous.completed_bytes
        ):
            raise ValueError("worker observation completed bytes cannot regress")
