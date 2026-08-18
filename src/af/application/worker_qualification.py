"""Measured evidence required to qualify one worker runtime profile."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from af.application.worker_generation import WorkerRuntimeOptimization
from af.framework.errors import InvalidProviderConfiguration
from af.framework.managers import CapabilityBinding

__all__ = (
    "WorkerOptimizationCandidate",
    "WorkerOptimizationPriorities",
    "WorkerOptimizationQualification",
    "WorkerOptimizationSelection",
    "WorkerOptimizationSelector",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_INDEX = 1_000_000
_MAX_CANDIDATES = 256


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"worker qualification {field} must be timezone-aware")
    return value.astimezone(UTC)


def _count(value: object, field: str, *, positive: bool = False) -> int:
    minimum = 1 if positive else 0
    if type(value) is not int or value < minimum:
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"worker qualification {field} must be {qualifier}")
    return value


def _index(value: object, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_INDEX:
        raise ValueError(f"worker optimization {field} must be an integer from zero to one million")
    return value


@dataclass(frozen=True, slots=True)
class WorkerOptimizationQualification:
    """Externally measured evidence and the exact runtime values it qualifies.

    This record does not execute or synthesize a benchmark. It binds an optimizer's
    selected runtime values to independently produced evidence for one concrete
    backend environment.
    """

    driver_name: str
    binding: CapabilityBinding
    optimization: WorkerRuntimeOptimization
    evidence_digest: str
    environment_digest: str
    observed_at: datetime
    expires_at: datetime
    sample_count: int
    real_service: bool
    correctness_failures: int
    settlement_failures: int
    reservation_losses: int

    def __post_init__(self) -> None:
        if type(self.driver_name) is not str or _IDENTIFIER.fullmatch(self.driver_name) is None:
            raise ValueError("worker qualification driver must be canonical")
        if not isinstance(self.binding, CapabilityBinding):
            raise TypeError("worker qualification binding is invalid")
        if not isinstance(self.optimization, WorkerRuntimeOptimization):
            raise TypeError("worker qualification runtime values are invalid")
        for value, field in (
            (self.evidence_digest, "evidence digest"),
            (self.environment_digest, "environment digest"),
        ):
            if type(value) is not str or _DIGEST.fullmatch(value) is None:
                raise ValueError(f"worker qualification {field} must be lowercase SHA-256")
        observed_at = _utc(self.observed_at, "observation time")
        expires_at = _utc(self.expires_at, "expiry time")
        if expires_at <= observed_at:
            raise ValueError("worker qualification expiry must follow observation")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "expires_at", expires_at)
        _count(self.sample_count, "sample count", positive=True)
        if type(self.real_service) is not bool:
            raise ValueError("worker qualification real-service flag must be boolean")
        _count(self.correctness_failures, "correctness failures")
        _count(self.settlement_failures, "settlement failures")
        _count(self.reservation_losses, "reservation losses")


@dataclass(frozen=True, slots=True)
class WorkerOptimizationPriorities:
    """Operator-selected integer percentages for each optimization objective."""

    performance: int
    safety: int
    cost: int

    def __post_init__(self) -> None:
        for value, field in (
            (self.performance, "performance priority"),
            (self.safety, "safety priority"),
            (self.cost, "cost priority"),
        ):
            if type(value) is not int or not 0 <= value <= 100:
                raise ValueError(f"worker optimization {field} must be an integer percentage")
        if self.performance + self.safety + self.cost != 100:
            raise ValueError("worker optimization priorities must total one hundred")


@dataclass(frozen=True, slots=True)
class WorkerOptimizationCandidate:
    """One qualified profile plus externally normalized objective indices."""

    qualification: WorkerOptimizationQualification
    performance_index: int
    safety_index: int
    cost_efficiency_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.qualification, WorkerOptimizationQualification):
            raise TypeError("worker optimization candidate qualification is invalid")
        _index(self.performance_index, "performance index")
        _index(self.safety_index, "safety index")
        _index(self.cost_efficiency_index, "cost-efficiency index")

    def weighted_score(self, priorities: WorkerOptimizationPriorities) -> int:
        if not isinstance(priorities, WorkerOptimizationPriorities):
            raise TypeError("worker optimization priorities are invalid")
        return (
            self.performance_index * priorities.performance
            + self.safety_index * priorities.safety
            + self.cost_efficiency_index * priorities.cost
        )


@dataclass(frozen=True, slots=True)
class WorkerOptimizationSelection:
    """Auditable result of deterministic objective-policy selection."""

    qualification: WorkerOptimizationQualification
    weighted_score: int
    candidate_count: int
    selection_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.qualification, WorkerOptimizationQualification):
            raise TypeError("worker optimization selection qualification is invalid")
        if type(self.weighted_score) is not int or self.weighted_score < 0:
            raise ValueError("worker optimization selection score is invalid")
        if (
            type(self.candidate_count) is not int
            or not 1 <= self.candidate_count <= _MAX_CANDIDATES
        ):
            raise ValueError("worker optimization selection candidate count is invalid")
        if (
            type(self.selection_digest) is not str
            or _DIGEST.fullmatch(self.selection_digest) is None
        ):
            raise ValueError("worker optimization selection digest must be lowercase SHA-256")


def _current_utc(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise InvalidProviderConfiguration("worker optimization selection time is invalid")
    return current.astimezone(UTC)


def _require_candidate_safety(
    qualification: WorkerOptimizationQualification,
    *,
    now: datetime,
) -> None:
    if qualification.observed_at > now:
        raise InvalidProviderConfiguration("worker optimization evidence is future-dated")
    if qualification.expires_at <= now:
        raise InvalidProviderConfiguration("worker optimization evidence is expired")
    if not qualification.real_service:
        raise InvalidProviderConfiguration("worker optimization evidence is not real-service")
    if (
        qualification.correctness_failures
        or qualification.settlement_failures
        or qualification.reservation_losses
    ):
        raise InvalidProviderConfiguration("worker optimization evidence has safety findings")


def _selection_digest(
    candidates: tuple[WorkerOptimizationCandidate, ...],
    priorities: WorkerOptimizationPriorities,
    selected: WorkerOptimizationCandidate,
) -> str:
    def duration_microseconds(value: timedelta | None) -> int | None:
        if value is None:
            return None
        return (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds

    def candidate_payload(candidate: WorkerOptimizationCandidate) -> dict[str, object]:
        qualification = candidate.qualification
        optimization = qualification.optimization
        return {
            "binding": asdict(qualification.binding),
            "cost_efficiency_index": candidate.cost_efficiency_index,
            "correctness_failures": qualification.correctness_failures,
            "driver_name": qualification.driver_name,
            "environment_digest": qualification.environment_digest,
            "evidence_digest": qualification.evidence_digest,
            "expires_at": qualification.expires_at.isoformat(),
            "observed_at": qualification.observed_at.isoformat(),
            "optimization": {
                "heartbeat_interval_us": duration_microseconds(optimization.heartbeat_interval),
                "max_concurrency": optimization.max_concurrency,
                "max_idle_time_us": duration_microseconds(optimization.max_idle_time),
                "poll_interval_us": duration_microseconds(optimization.poll_interval),
                "queue_wait_alert_after_us": duration_microseconds(
                    optimization.queue_wait_alert_after
                ),
                "renewal_max_interval_us": duration_microseconds(optimization.renewal_max_interval),
                "renewal_min_interval_us": duration_microseconds(optimization.renewal_min_interval),
                "worker_count": optimization.worker_count,
            },
            "performance_index": candidate.performance_index,
            "real_service": qualification.real_service,
            "reservation_losses": qualification.reservation_losses,
            "safety_index": candidate.safety_index,
            "sample_count": qualification.sample_count,
            "settlement_failures": qualification.settlement_failures,
        }

    payload = {
        "candidates": [
            candidate_payload(candidate)
            for candidate in sorted(candidates, key=lambda item: item.qualification.evidence_digest)
        ],
        "priorities": {
            "cost": priorities.cost,
            "performance": priorities.performance,
            "safety": priorities.safety,
        },
        "selected_evidence_digest": selected.qualification.evidence_digest,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WorkerOptimizationSelector:
    """Select one safe exact-target candidate using explicit operator priorities."""

    def select(
        self,
        candidates: Iterable[WorkerOptimizationCandidate],
        *,
        priorities: WorkerOptimizationPriorities,
        now: datetime | None = None,
    ) -> WorkerOptimizationSelection:
        if not isinstance(priorities, WorkerOptimizationPriorities):
            raise TypeError("worker optimization priorities are invalid")
        try:
            available = tuple(candidates)
        except TypeError as error:
            raise TypeError("worker optimization candidates are invalid") from error
        if not 1 <= len(available) <= _MAX_CANDIDATES:
            raise InvalidProviderConfiguration(
                "worker optimization requires a bounded non-empty candidate set"
            )
        current = _current_utc(now)
        first_target: tuple[object, ...] | None = None
        evidence_digests: set[str] = set()
        for candidate in available:
            if not isinstance(candidate, WorkerOptimizationCandidate):
                raise TypeError("worker optimization candidates are invalid")
            qualification = candidate.qualification
            _require_candidate_safety(qualification, now=current)
            target = (
                qualification.driver_name,
                qualification.binding,
                qualification.environment_digest,
            )
            if first_target is None:
                first_target = target
            elif target != first_target:
                raise InvalidProviderConfiguration(
                    "worker optimization candidates must qualify the same target"
                )
            if qualification.evidence_digest in evidence_digests:
                raise InvalidProviderConfiguration(
                    "worker optimization candidate evidence must be unique"
                )
            evidence_digests.add(qualification.evidence_digest)

        selected = min(
            available,
            key=lambda candidate: (
                -candidate.weighted_score(priorities),
                -candidate.safety_index,
                -candidate.performance_index,
                -candidate.cost_efficiency_index,
                candidate.qualification.evidence_digest,
            ),
        )
        return WorkerOptimizationSelection(
            qualification=selected.qualification,
            weighted_score=selected.weighted_score(priorities),
            candidate_count=len(available),
            selection_digest=_selection_digest(available, priorities, selected),
        )
