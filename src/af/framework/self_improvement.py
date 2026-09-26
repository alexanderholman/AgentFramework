"""Pure bounded self-operation dispatch over exact admitted stage contracts."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from af.framework.self_operation import (
    SelfOperationSnapshot,
    SelfOperationStage,
    StageHandoff,
    StageReason,
    StageResult,
)
from af.framework.workflows import CheckpointModel


class AdmittedStage(CheckpointModel):
    handoff: StageHandoff
    result: StageResult
    reviewer: str = Field(min_length=1, max_length=128)
    independent: bool = False
    expires_at: datetime

    @model_validator(mode="after")
    def exact_binding(self) -> Self:
        if not self.independent:
            raise ValueError("stage admission requires independent review evidence")
        if (
            self.result.stage != self.handoff.stage
            or self.result.handoff_digest != self.handoff.digest
            or self.result.reason != StageReason.SELECTED
        ):
            raise ValueError("admission must bind exact selected stage result")
        if self.expires_at.utcoffset() is None:
            raise ValueError("admission expiry must be timezone aware")
        return self


class ImprovementSnapshot(CheckpointModel):
    source: SelfOperationSnapshot
    handoffs: tuple[StageHandoff, ...]
    admissions: tuple[AdmittedStage, ...] = ()

    @model_validator(mode="after")
    def full_lifecycle(self) -> Self:
        if tuple(handoff.stage for handoff in self.handoffs) != tuple(SelfOperationStage):
            raise ValueError("snapshot requires exactly one handoff for each ordered stage")
        return self

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class ImprovementBudget(CheckpointModel):
    deadline: datetime
    maximum_stages: int = Field(default=10, ge=1, le=100)
    maximum_cost: int = Field(default=100, ge=1, le=1_000_000)
    maximum_no_progress: int = Field(default=2, ge=1, le=10)

    @model_validator(mode="after")
    def aware_deadline(self) -> Self:
        if self.deadline.utcoffset() is None:
            raise ValueError("improvement deadline must be timezone aware")
        return self


class StageDispatch(CheckpointModel):
    operation_id: str = Field(min_length=1, max_length=128)
    stage: SelfOperationStage
    handoff: StageHandoff
    snapshot_digest: str
    effect_key: str


class StageEvaluation(CheckpointModel):
    result: StageResult
    cost: int = Field(default=1, ge=0, le=1_000_000)
    admission: AdmittedStage | None = None
    affected_stage: SelfOperationStage | None = None

    @model_validator(mode="after")
    def admission_binding(self) -> Self:
        if self.admission is not None and self.admission.result != self.result:
            raise ValueError("stage admission must name the exact result")
        return self


class ImprovementRecord(CheckpointModel):
    dispatch: StageDispatch
    kind: Literal["reserved", "completed"]
    evaluation: StageEvaluation | None = None

    @model_validator(mode="after")
    def completion_binding(self) -> Self:
        if (self.kind == "completed") != (self.evaluation is not None):
            raise ValueError("only completed records contain evaluations")
        if self.evaluation is not None and (
            self.evaluation.result.handoff_digest != self.dispatch.handoff.digest
            or self.evaluation.result.stage != self.dispatch.stage
        ):
            raise ValueError("completion must bind the exact dispatched handoff")
        return self


class ImprovementStop(CheckpointModel):
    operation_id: str
    reason: str
    stage: SelfOperationStage | None = None
    affected_stage: SelfOperationStage | None = None


class SelfImprovementStagePort(Protocol):
    def invoke(self, dispatch: StageDispatch, snapshot: ImprovementSnapshot) -> StageEvaluation: ...


class SelfImprovementSnapshotPort(Protocol):
    def capture(self, operation_id: str) -> ImprovementSnapshot: ...


class SelfImprovementLedger(Protocol):
    def records(self, operation_id: str) -> tuple[ImprovementRecord, ...]: ...
    def reserve(self, dispatch: StageDispatch) -> bool: ...
    def complete(self, dispatch: StageDispatch, evaluation: StageEvaluation) -> None: ...


def next_improvement_stage(
    operation_id: str,
    snapshot: ImprovementSnapshot,
    records: tuple[ImprovementRecord, ...],
    budget: ImprovementBudget,
    *,
    now: datetime,
    cancelled: bool = False,
) -> StageDispatch | ImprovementStop:
    def stop(
        reason: str,
        stage: SelfOperationStage | None = None,
        affected: SelfOperationStage | None = None,
    ) -> ImprovementStop:
        return ImprovementStop(
            operation_id=operation_id, reason=reason, stage=stage, affected_stage=affected
        )

    if now.utcoffset() is None:
        raise ValueError("evaluation time must be timezone aware")
    if cancelled:
        return stop("cancelled")
    if now >= budget.deadline:
        return stop("deadline_exhausted")
    if any(record.dispatch.operation_id != operation_id for record in records):
        return stop("ledger_operation_mismatch")
    completed = tuple(record for record in records if record.kind == "completed")
    done_keys = {record.dispatch.effect_key for record in completed}
    if any(
        record.kind == "reserved" and record.dispatch.effect_key not in done_keys
        for record in records
    ):
        return stop("interrupted_effect_requires_recovery")
    for record in completed:
        evaluation = record.evaluation
        assert evaluation is not None
        if evaluation.result.reason != StageReason.SELECTED:
            affected = evaluation.affected_stage
            if affected is not None and list(SelfOperationStage).index(affected) > list(
                SelfOperationStage
            ).index(record.dispatch.stage):
                return stop("invalid_backward_evidence", record.dispatch.stage)
            return stop(evaluation.result.reason.value, record.dispatch.stage, affected)
        if evaluation.admission is None:
            return stop("result_not_independently_admitted", record.dispatch.stage)
    admissions = (
        *snapshot.admissions,
        *(
            record.evaluation.admission
            for record in completed
            if record.evaluation is not None and record.evaluation.admission is not None
        ),
    )
    unmet: StageHandoff | None = None
    predecessor: str | None = None
    for handoff in snapshot.handoffs:
        matches = [
            admission
            for admission in admissions
            if admission.handoff.digest == handoff.digest and admission.expires_at > now
        ]
        if matches:
            if len({match.result.digest for match in matches}) != 1:
                return stop("ambiguous_admission", handoff.stage)
            predecessor = matches[0].result.digest
            continue
        unmet = handoff
        break
    if unmet is None:
        return stop("complete")
    if len(completed) >= budget.maximum_stages:
        return stop("stage_budget_exhausted", unmet.stage)
    if (
        sum(record.evaluation.cost for record in completed if record.evaluation is not None)
        >= budget.maximum_cost
    ):
        return stop("cost_budget_exhausted", unmet.stage)
    if completed and (
        completed[-1].dispatch.snapshot_digest == snapshot.digest
        or completed[-1].dispatch.handoff.source_snapshot_digest == snapshot.source.digest
    ):
        return stop("fresh_snapshot_required", unmet.stage)
    if completed and completed[0].dispatch.handoff.policy_digest != unmet.policy_digest:
        return stop("policy_changed", unmet.stage)
    attempts = sum(record.dispatch.stage == unmet.stage for record in completed)
    if attempts >= budget.maximum_no_progress:
        return stop("no_progress", unmet.stage)
    if unmet.source_snapshot_digest != snapshot.source.digest:
        return stop("stale_snapshot", unmet.stage)
    if unmet.predecessor_result_digest is not None:
        if unmet.predecessor_result_digest != predecessor:
            return stop("predecessor_not_admitted", unmet.stage)
    elif not any(
        authority.authority_id == unmet.entry_authority_id and authority.granted
        for authority in snapshot.source.authorities
    ):
        return stop("missing_authority", unmet.stage)
    if not snapshot.source.budgets or any(
        item.used >= item.limit for item in snapshot.source.budgets
    ):
        return stop("snapshot_budget_exhausted", unmet.stage)
    key_material = f"{operation_id}:{snapshot.digest}:{unmet.digest}"
    return StageDispatch(
        operation_id=operation_id,
        stage=unmet.stage,
        handoff=unmet,
        snapshot_digest=snapshot.digest,
        effect_key=hashlib.sha256(key_material.encode()).hexdigest(),
    )
