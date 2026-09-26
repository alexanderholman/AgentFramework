"""Pure longitudinal evolution planning: evidence proposes, never grants authority."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from af.framework.workflows import CheckpointModel


class EvolutionObservation(CheckpointModel):
    period_start: datetime
    period_end: datetime
    baseline: float
    observed: float
    uncertainty: float = Field(ge=0)
    samples: int = Field(ge=1, le=1_000_000)
    held_out: bool = False
    evidence_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    sensitive: bool = False
    confounded: bool = False
    admitted: bool = False

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.period_start.utcoffset() is None or self.period_end.utcoffset() is None:
            raise ValueError("evolution evidence periods must be timezone aware")
        if self.period_end <= self.period_start:
            raise ValueError("evolution evidence interval must be positive")
        return self


class EvolutionCandidate(CheckpointModel):
    change: str = Field(min_length=1, max_length=512)
    artifact_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    predicted_gain: tuple[float, float]
    rollback: str = Field(min_length=1, max_length=512)
    alternatives: tuple[str, ...] = Field(min_length=1, max_length=16)
    guardrails: tuple[str, ...] = Field(min_length=1, max_length=16)
    counterfactual_digest: str | None = Field(default=None, pattern=r"^sha256:[a-f0-9]{64}$")
    reversible: bool = True
    changes_objective_or_policy: bool = False
    goodhart_checked: bool = False
    estimated_cost: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def effect_interval(self) -> Self:
        if not 0 < self.predicted_gain[0] <= self.predicted_gain[1]:
            raise ValueError("candidate gain must be a positive ordered interval")
        return self


class EvolutionSnapshot(CheckpointModel):
    objective_id: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=128)
    direction: Literal["minimize", "maximize"]
    captured_at: datetime
    expires_at: datetime
    policy_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    observations: tuple[EvolutionObservation, ...] = Field(max_length=256)
    candidate: EvolutionCandidate | None = None
    minimum_samples: int = Field(default=30, ge=1)
    minimum_effect: float = Field(default=0, ge=0)
    maximum_cost: float = Field(default=100, ge=0)

    @model_validator(mode="after")
    def valid_times(self) -> Self:
        if self.captured_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("evolution snapshot times must be timezone aware")
        if self.expires_at <= self.captured_at:
            raise ValueError("evolution expiry must follow snapshot time")
        return self

    def canonical_json(self) -> str:
        value = self.model_dump(mode="json")
        value["observations"] = sorted(
            value["observations"], key=lambda item: (item["period_start"], item["evidence_digest"])
        )
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json().encode()).hexdigest()


class EvolutionPlan(CheckpointModel):
    objective_id: str
    snapshot_digest: str
    policy_digest: str
    artifact_digest: str
    change: str
    evidence: tuple[str, ...]
    observed_gap: tuple[float, float]
    predicted_effect: tuple[float, float]
    alternatives: tuple[str, ...]
    metric: str
    guardrails: tuple[str, ...]
    rollback: str
    rollback_trigger: str
    expires_at: datetime
    validation: str = "held-out time-separated baseline comparison"
    authority: Literal["proposal-only"] = "proposal-only"

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json().encode()).hexdigest()


class EvolutionStop(CheckpointModel):
    reason: str
    snapshot_digest: str

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


class EvolutionAdmission(CheckpointModel):
    proposal_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    reviewer: str = Field(min_length=1, max_length=128)
    independent: bool
    admitted: bool
    expires_at: datetime

    @model_validator(mode="after")
    def aware_expiry(self) -> Self:
        if self.expires_at.utcoffset() is None:
            raise ValueError("evolution admission expiry must be timezone aware")
        return self


def admitted_evolution_objective(
    plan: EvolutionPlan, admission: EvolutionAdmission, *, now: datetime
) -> str:
    """Return an objective binding only after exact independent admission."""
    if (
        not admission.admitted
        or not admission.independent
        or admission.proposal_digest != plan.digest
        or min(plan.expires_at, admission.expires_at) <= now
    ):
        raise ValueError("evolution proposal lacks current independent exact-digest admission")
    return plan.digest


def plan_evolution(snapshot: EvolutionSnapshot) -> EvolutionPlan | EvolutionStop:
    def stop(reason: str) -> EvolutionStop:
        return EvolutionStop(reason=reason, snapshot_digest=snapshot.digest)

    observations = sorted(snapshot.observations, key=lambda item: item.period_start)
    if any(item.sensitive for item in observations):
        return stop("sensitive_evidence")
    if any(item.confounded for item in observations):
        return stop("confounded_evidence")
    if any(not item.admitted for item in observations):
        return stop("unadmitted_evidence")
    if any(item.samples < snapshot.minimum_samples for item in observations):
        return stop("weak_evidence")
    training = [item for item in observations if not item.held_out]
    holdout = [item for item in observations if item.held_out]
    if len(training) < 3 or not holdout:
        return stop("insufficient_longitudinal_evidence")
    if (
        any(
            first.period_end > second.period_start
            for first, second in itertools.pairwise(observations)
        )
        or min(item.period_start for item in holdout) < max(item.period_end for item in training)
        or max(item.period_end for item in observations) > snapshot.captured_at
    ):
        return stop("time_partition_invalid")
    direction = 1 if snapshot.direction == "minimize" else -1
    intervals = [
        (
            (item.observed - item.baseline) * direction - item.uncertainty,
            (item.observed - item.baseline) * direction + item.uncertainty,
        )
        for item in observations
    ]
    if any(lower <= snapshot.minimum_effect for lower, _upper in intervals):
        return stop("no_persistent_material_gap")
    candidate = snapshot.candidate
    if candidate is None:
        return stop("no_bounded_candidate")
    if candidate.changes_objective_or_policy or not candidate.reversible:
        return stop("unsafe_objective_or_irreversible_change")
    if candidate.counterfactual_digest is None:
        return stop("causal_alternatives_unchecked")
    if not candidate.goodhart_checked:
        return stop("goodhart_review_missing")
    if candidate.estimated_cost > snapshot.maximum_cost:
        return stop("cost_budget_exhausted")
    return EvolutionPlan(
        objective_id=snapshot.objective_id,
        snapshot_digest=snapshot.digest,
        policy_digest=snapshot.policy_digest,
        artifact_digest=candidate.artifact_digest,
        change=candidate.change,
        evidence=tuple(
            sorted(
                {item.evidence_digest for item in observations} | {candidate.counterfactual_digest}
            )
        ),
        observed_gap=(min(low for low, _ in intervals), max(high for _, high in intervals)),
        predicted_effect=candidate.predicted_gain,
        alternatives=candidate.alternatives,
        metric=snapshot.metric,
        guardrails=candidate.guardrails,
        rollback=candidate.rollback,
        rollback_trigger="held-out gain below lower prediction bound or any guardrail regression",
        expires_at=snapshot.expires_at,
    )
