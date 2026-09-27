"""Real read-only lifecycle rules over governed, exact-digest stage evidence.

These planners emit artifacts for independent review. A selected plan grants no
repository mutation, merge, release, or admission authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Self, cast

from pydantic import Field, JsonValue, model_validator

from af.framework.self_improvement import AdmittedStage
from af.framework.self_operation import (
    SelfOperationSnapshot,
    SelfOperationStage,
    StageHandoff,
    StagePlan,
    StageReason,
    StageResult,
)
from af.framework.workflows import CheckpointModel

_DIGEST = r"^sha256:[a-f0-9]{64}$"


class PriorityCandidate(CheckpointModel):
    candidate_id: str = Field(min_length=1, max_length=128)
    objective_id: str = Field(min_length=1, max_length=128)
    value: int = Field(default=0, ge=0, le=1000)
    urgency: int = Field(default=0, ge=0, le=1000)
    dependency_impact: int = Field(default=0, ge=0, le=1000)
    risk: int = Field(default=0, ge=0, le=1000)
    cost: int = Field(default=0, ge=0, le=1000)
    age: int = Field(default=0, ge=0, le=1000)
    aligned: bool = False
    blocked: bool = False
    active_overlap: bool = False
    evidence_digest: str = Field(pattern=_DIGEST)

    @property
    def score(self) -> int:
        return (
            self.value * 5
            + self.urgency * 3
            + self.dependency_impact * 2
            + self.age
            - self.risk * 4
            - self.cost
        )


class WorkItem(CheckpointModel):
    item_id: str = Field(min_length=1, max_length=128)
    objective_id: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    acceptance_ids: tuple[str, ...] = Field(default=(), max_length=128)
    dependencies: tuple[str, ...] = Field(default=(), max_length=128)


class GateEvidence(CheckpointModel):
    name: str = Field(min_length=1, max_length=128)
    subject_digest: str = Field(pattern=_DIGEST)
    evidence_digest: str = Field(pattern=_DIGEST)
    passed: bool
    reviewer: str = Field(min_length=1, max_length=128)
    independent: bool = False
    expires_at: datetime

    @model_validator(mode="after")
    def aware_expiry(self) -> Self:
        if self.expires_at.utcoffset() is None:
            raise ValueError("gate expiry must be timezone aware")
        return self


class MetricEvidence(CheckpointModel):
    name: str = Field(min_length=1, max_length=128)
    baseline: float
    candidate: float
    uncertainty: float = Field(ge=0)
    direction: Literal["minimize", "maximize"]
    methodology: str = Field(min_length=1, max_length=512)
    evidence_digest: str = Field(pattern=_DIGEST)


class StageEvidenceSnapshot(CheckpointModel):
    source: SelfOperationSnapshot
    handoff: StageHandoff
    evaluated_at: datetime
    policy_digest: str = Field(pattern=_DIGEST)
    objective_id: str = Field(min_length=1, max_length=128)
    acceptance_clauses: tuple[str, ...] = Field(min_length=1, max_length=128)
    predecessor: AdmittedStage | None = None
    candidates: tuple[PriorityCandidate, ...] = Field(default=(), max_length=128)
    work_items: tuple[WorkItem, ...] = Field(default=(), max_length=128)
    invariants: tuple[str, ...] = Field(default=(), max_length=128)
    non_goals: tuple[str, ...] = Field(default=(), max_length=128)
    gates: tuple[GateEvidence, ...] = Field(default=(), max_length=128)
    required_gates: tuple[str, ...] = ()
    implementation_owner: str | None = None
    environment: str | None = None
    qualification_tier: Literal["development", "local", "release-candidate"] = "development"
    metrics: tuple[MetricEvidence, ...] = Field(default=(), max_length=64)
    accepted_contract_digest: str | None = Field(default=None, pattern=_DIGEST)
    candidate_contract_digest: str | None = Field(default=None, pattern=_DIGEST)

    @model_validator(mode="after")
    def bounded_unique_collections(self) -> Self:
        if self.evaluated_at.utcoffset() is None:
            raise ValueError("stage evaluation time must be timezone aware")
        for values in (
            self.acceptance_clauses,
            self.required_gates,
            tuple(item.candidate_id for item in self.candidates),
            tuple(item.item_id for item in self.work_items),
            tuple(item.name for item in self.gates),
            tuple(item.name for item in self.metrics),
        ):
            if len(values) != len(set(values)):
                raise ValueError("stage evidence identities must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ReadOnlyStageEvaluation:
    plan: StagePlan
    artifact_json: str | None

    @property
    def artifact(self) -> dict[str, JsonValue] | None:
        return (
            None
            if self.artifact_json is None
            else cast(dict[str, JsonValue], json.loads(self.artifact_json))
        )

    def canonical_json(self) -> str:
        return json.dumps(
            {"plan": self.plan.payload(), "artifact": self.artifact},
            sort_keys=True,
            separators=(",", ":"),
        )


def _result(
    snapshot: StageEvidenceSnapshot,
    reason: StageReason,
    artifact: dict[str, JsonValue] | None = None,
) -> ReadOnlyStageEvaluation:
    serialized = (
        None if artifact is None else json.dumps(artifact, sort_keys=True, separators=(",", ":"))
    )
    digest = (
        None if serialized is None else "sha256:" + hashlib.sha256(serialized.encode()).hexdigest()
    )
    stage = snapshot.handoff.stage
    result = StageResult(stage, snapshot.handoff.digest, reason, digest)
    return ReadOnlyStageEvaluation(
        StagePlan(
            stage,
            snapshot.handoff,
            result,
            "stop" if artifact is None else f"plan-{stage.value}",
            (),
            ("snapshot-changed", "authority-revoked", "budget-exhausted"),
        ),
        serialized,
    )


def _admissible(snapshot: StageEvidenceSnapshot) -> StageReason | None:
    handoff = snapshot.handoff
    if (
        handoff.source_snapshot_digest != snapshot.source.digest
        or snapshot.source.captured_at > snapshot.evaluated_at
        or snapshot.evaluated_at - snapshot.source.captured_at > timedelta(minutes=15)
    ):
        return StageReason.STALE_SNAPSHOT
    if handoff.policy_digest != snapshot.policy_digest:
        return StageReason.POLICY_DENIED
    if not snapshot.source.budgets or any(
        item.used >= item.limit for item in snapshot.source.budgets
    ):
        return StageReason.NO_ELIGIBLE_INPUT
    if handoff.predecessor_result_digest is not None:
        predecessor = snapshot.predecessor
        stages = tuple(SelfOperationStage)
        index = stages.index(handoff.stage)
        if (
            predecessor is None
            or not predecessor.independent
            or predecessor.expires_at <= snapshot.evaluated_at
            or predecessor.result.digest != handoff.predecessor_result_digest
            or predecessor.handoff.policy_digest != snapshot.policy_digest
            or index == 0
            or predecessor.result.stage != stages[index - 1]
        ):
            return StageReason.MISSING_EVIDENCE
    elif not any(
        authority.authority_id == handoff.entry_authority_id
        and authority.granted
        and authority.scope_id in {f"self-{handoff.stage.value}", "self-operation:entry"}
        for authority in snapshot.source.authorities
    ):
        return StageReason.MISSING_AUTHORITY
    return None


def _scope(snapshot: StageEvidenceSnapshot) -> tuple[list[str], list[str]] | None:
    items = {item.item_id: item for item in snapshot.work_items}
    acceptance = set(snapshot.acceptance_clauses)
    selected = {
        item.item_id
        for item in items.values()
        if item.objective_id == snapshot.objective_id
        and item.acceptance_ids
        and set(item.acceptance_ids) <= acceptance
    }
    covered = {
        clause
        for item in items.values()
        if item.item_id in selected
        for clause in item.acceptance_ids
    }
    if covered != acceptance:
        return None
    while True:
        dependencies = {
            dependency for identity in selected for dependency in items[identity].dependencies
        }
        if any(
            dependency not in items
            or items[dependency].objective_id != snapshot.objective_id
            or not set(items[dependency].acceptance_ids) <= acceptance
            for dependency in dependencies
        ):
            return None
        if dependencies <= selected:
            break
        selected.update(dependencies)
    pending = {identity: set(items[identity].dependencies) for identity in selected}
    visited: set[str] = set()
    while pending:
        ready = {identity for identity, dependencies in pending.items() if dependencies <= visited}
        if not ready:
            return None
        visited.update(ready)
        pending = {
            identity: dependencies
            for identity, dependencies in pending.items()
            if identity not in ready
        }
    return sorted(selected), sorted(set(items) - selected)


def _gates(snapshot: StageEvidenceSnapshot, required: set[str]) -> StageReason | None:
    evidence = {item.name: item for item in snapshot.gates}
    if not required <= set(evidence):
        return StageReason.MISSING_EVIDENCE
    for name in required:
        gate = evidence[name]
        if (
            gate.subject_digest != snapshot.handoff.input_artifact_digest
            or gate.evidence_digest not in snapshot.source.evidence_digests
            or gate.expires_at <= snapshot.evaluated_at
        ):
            return StageReason.MISSING_EVIDENCE
        if (
            not gate.passed
            or not gate.independent
            or gate.reviewer == snapshot.implementation_owner
        ):
            return StageReason.POLICY_DENIED
    return None


def evaluate_stage(snapshot: StageEvidenceSnapshot) -> ReadOnlyStageEvaluation:
    denial = _admissible(snapshot)
    if denial is not None:
        return _result(snapshot, denial)
    stage = snapshot.handoff.stage
    base: dict[str, JsonValue] = {
        "objective_id": snapshot.objective_id,
        "acceptance": cast(JsonValue, sorted(snapshot.acceptance_clauses)),
        "grants_authority": False,
    }
    if stage == SelfOperationStage.PRIORITISATION:
        eligible = [
            item
            for item in snapshot.candidates
            if item.aligned
            and not item.blocked
            and not item.active_overlap
            and item.objective_id == snapshot.objective_id
            and item.evidence_digest in snapshot.source.evidence_digests
            and item.score > 0
        ]
        if not eligible:
            return _result(snapshot, StageReason.NO_ELIGIBLE_INPUT)
        score = max(item.score for item in eligible)
        winners = [item for item in eligible if item.score == score]
        if len(winners) != 1:
            return _result(snapshot, StageReason.AMBIGUOUS_SELECTION)
        base.update(
            {
                "selected": winners[0].candidate_id,
                "score": score,
                "scoring": "5*value+3*urgency+2*dependency_impact+age-4*risk-cost",
            }
        )
    elif stage in {SelfOperationStage.SPECIFICATION, SelfOperationStage.CONTAINMENT}:
        scope = _scope(snapshot)
        if scope is None:
            return _result(snapshot, StageReason.MISSING_EVIDENCE)
        retained, adjacent = scope
        base.update({"retained": cast(JsonValue, retained), "adjacent": cast(JsonValue, adjacent)})
        if stage == SelfOperationStage.SPECIFICATION:
            if not snapshot.invariants or not snapshot.non_goals:
                return _result(snapshot, StageReason.MISSING_EVIDENCE)
            base.update(
                {
                    "invariants": cast(JsonValue, sorted(snapshot.invariants)),
                    "non_goals": cast(JsonValue, sorted(snapshot.non_goals)),
                    "requirements": cast(
                        JsonValue,
                        [
                            item.model_dump(mode="json")
                            for item in sorted(snapshot.work_items, key=lambda item: item.item_id)
                            if item.item_id in retained
                        ],
                    ),
                }
            )
    elif stage in {SelfOperationStage.ASSURANCE, SelfOperationStage.QUALIFICATION}:
        required = {
            "tests",
            "security",
            "compatibility",
            "provenance",
            "rollback",
            "governance",
        } | set(snapshot.required_gates)
        if stage == SelfOperationStage.QUALIFICATION:
            required.add("environment")
            if not snapshot.environment:
                return _result(snapshot, StageReason.MISSING_EVIDENCE)
        denial = _gates(snapshot, required)
        if denial is not None:
            return _result(snapshot, denial)
        base.update(
            {
                "verified_gates": cast(JsonValue, sorted(required)),
                "subject_digest": snapshot.handoff.input_artifact_digest,
            }
        )
        if stage == SelfOperationStage.QUALIFICATION:
            base.update(
                {
                    "environment": snapshot.environment,
                    "qualification_tier": snapshot.qualification_tier,
                }
            )
    elif stage == SelfOperationStage.OPTIMISATION:
        if (
            snapshot.accepted_contract_digest is None
            or snapshot.accepted_contract_digest != snapshot.candidate_contract_digest
        ):
            return _result(snapshot, StageReason.POLICY_DENIED)
        denial = _gates(
            snapshot, {"semantic-equivalence", "benchmark"} | set(snapshot.required_gates)
        )
        if denial is not None:
            return _result(snapshot, denial)
        if not snapshot.metrics:
            return _result(snapshot, StageReason.MISSING_EVIDENCE)
        gains = []
        for metric in sorted(snapshot.metrics, key=lambda item: item.name):
            if metric.evidence_digest not in snapshot.source.evidence_digests:
                return _result(snapshot, StageReason.MISSING_EVIDENCE)
            gain = (metric.baseline - metric.candidate) * (
                1 if metric.direction == "minimize" else -1
            )
            if gain <= 2 * metric.uncertainty:
                return _result(snapshot, StageReason.NO_ELIGIBLE_INPUT)
            gains.append(
                {
                    "metric": metric.name,
                    "gain_lower": gain - 2 * metric.uncertainty,
                    "methodology": metric.methodology,
                }
            )
        base.update(
            {"contract_digest": snapshot.accepted_contract_digest, "gains": cast(JsonValue, gains)}
        )
    else:
        return _result(snapshot, StageReason.NO_ELIGIBLE_INPUT)
    return _result(snapshot, StageReason.SELECTED, base)
