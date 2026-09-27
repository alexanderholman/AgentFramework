"""Shared real read-only stage evaluation and explicit independent review adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from af.framework.self_improvement import (
    AdmittedStage,
    ImprovementSnapshot,
    StageDispatch,
    StageEvaluation,
)
from af.framework.self_operation import SelfOperationStage
from af.framework.self_stages import ReadOnlyStageEvaluation, StageEvidenceSnapshot, evaluate_stage


class SelfStageController:
    def __init__(self, source: Callable[[SelfOperationStage], StageEvidenceSnapshot] | None = None):
        self._source = source

    def evaluate(
        self, stage: SelfOperationStage, snapshot: StageEvidenceSnapshot | None = None
    ) -> ReadOnlyStageEvaluation:
        if snapshot is None:
            if self._source is None:
                raise ValueError("stage evidence is not configured; provide an exact snapshot")
            snapshot = self._source(stage)
        if snapshot.handoff.stage != stage:
            raise ValueError("snapshot handoff does not match the requested lifecycle stage")
        return evaluate_stage(snapshot)


class StageEvidenceSource(Protocol):
    def load(
        self, dispatch: StageDispatch, snapshot: ImprovementSnapshot
    ) -> StageEvidenceSnapshot: ...


class IndependentStageReview(Protocol):
    def admit(self, evaluation: ReadOnlyStageEvaluation) -> AdmittedStage | None: ...


class EvidenceSelfImprovementStagePort:
    """Bind actual typed evidence planners to the bounded dispatcher.

    No reviewer means no admission; a planner never silently approves its own
    artifact. Existing analysis/design/development adapters remain separate ports.
    """

    def __init__(
        self, source: StageEvidenceSource, review: IndependentStageReview | None = None
    ) -> None:
        self._source = source
        self._review = review

    def invoke(self, dispatch: StageDispatch, snapshot: ImprovementSnapshot) -> StageEvaluation:
        evidence = self._source.load(dispatch, snapshot)
        if (
            dispatch.snapshot_digest != snapshot.digest
            or evidence.source.digest != snapshot.source.digest
            or evidence.handoff.digest != dispatch.handoff.digest
        ):
            raise ValueError("stage evidence does not match the dispatched exact snapshot")
        result = evaluate_stage(evidence)
        admission = None
        if self._review is not None and result.plan.result.output_digest is not None:
            admission = self._review.admit(result)
        return StageEvaluation(result=result.plan.result, admission=admission)
