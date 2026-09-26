from __future__ import annotations

from datetime import UTC, datetime, timedelta

from af.framework.self_improvement import (
    ImprovementBudget,
    ImprovementSnapshot,
    ImprovementStop,
    next_improvement_stage,
)
from af.framework.self_operation import (
    AuthorityRecord,
    SelfOperationSnapshot,
    SelfOperationStage,
    StageBudget,
    StageHandoff,
)

D = "sha256:" + "a" * 64
NOW = datetime(2026, 9, 26, tzinfo=UTC)


def snapshot():
    source = SelfOperationSnapshot(
        "fixture",
        "owner/repo",
        "main",
        "a" * 40,
        NOW,
        (D,),
        (AuthorityRecord("local", "self-improve", True),),
        (StageBudget("cycles", "count", 10, 0),),
    )
    handoffs = tuple(
        StageHandoff(stage, source.digest, D, D, None, "local") for stage in SelfOperationStage
    )
    return ImprovementSnapshot(source=source, handoffs=handoffs, admissions=())


def test_dispatch_starts_at_earliest_unmet_gate_and_is_deterministic():
    value = snapshot()
    budget = ImprovementBudget(deadline=NOW + timedelta(hours=1))
    first = next_improvement_stage("fixture-1", value, (), budget, now=NOW)
    assert first.stage == SelfOperationStage.ANALYSIS
    assert (
        first.model_dump_json()
        == next_improvement_stage("fixture-1", value, (), budget, now=NOW).model_dump_json()
    )


def test_deadline_and_cancellation_have_zero_stage_dispatch():
    value = snapshot()
    budget = ImprovementBudget(deadline=NOW)
    result = next_improvement_stage("fixture-1", value, (), budget, now=NOW)
    assert isinstance(result, ImprovementStop)
    assert result.reason == "deadline_exhausted"
    result = next_improvement_stage("fixture-1", value, (), budget, now=NOW, cancelled=True)
    assert result.reason == "cancelled"


def admitted(snapshot_value, stage):
    from af.framework.self_improvement import AdmittedStage
    from af.framework.self_operation import StageReason, StageResult

    handoff = next(item for item in snapshot_value.handoffs if item.stage == stage)
    result = StageResult(stage, handoff.digest, StageReason.SELECTED, D)
    return AdmittedStage(
        independent=True,
        handoff=handoff,
        result=result,
        reviewer="reviewer",
        expires_at=NOW + timedelta(hours=1),
    )


def test_skips_only_exact_current_admitted_stage():
    value = snapshot()
    admission = admitted(value, SelfOperationStage.ANALYSIS)
    value = value.model_copy(update={"admissions": (admission,)})
    budget = ImprovementBudget(deadline=NOW + timedelta(hours=1))
    step = next_improvement_stage("fixture", value, (), budget, now=NOW)
    assert step.stage == SelfOperationStage.ALIGNMENT
    expired = admission.model_copy(update={"expires_at": NOW})
    step = next_improvement_stage(
        "fixture", value.model_copy(update={"admissions": (expired,)}), (), budget, now=NOW
    )
    assert step.stage == SelfOperationStage.ANALYSIS


def test_stop_propagates_backward_evidence_without_downstream_dispatch():
    from af.framework.self_improvement import ImprovementRecord, StageEvaluation
    from af.framework.self_operation import StageReason, StageResult

    value = snapshot()
    budget = ImprovementBudget(deadline=NOW + timedelta(hours=1))
    dispatch = next_improvement_stage("fixture", value, (), budget, now=NOW)
    result = StageResult(
        dispatch.stage, dispatch.handoff.digest, StageReason.MISSING_EVIDENCE, None
    )
    record = ImprovementRecord(
        dispatch=dispatch,
        kind="completed",
        evaluation=StageEvaluation(result=result, affected_stage=SelfOperationStage.ANALYSIS),
    )
    stop = next_improvement_stage("fixture", value, (record,), budget, now=NOW)
    assert stop.reason == "missing_evidence"
    assert stop.affected_stage == SelfOperationStage.ANALYSIS


def test_unadmitted_result_and_fresh_snapshot_bounds():
    from af.framework.self_improvement import ImprovementRecord, StageEvaluation

    value = snapshot()
    budget = ImprovementBudget(deadline=NOW + timedelta(hours=1))
    dispatch = next_improvement_stage("fixture", value, (), budget, now=NOW)
    admission = admitted(value, dispatch.stage)
    record = ImprovementRecord(
        dispatch=dispatch, kind="completed", evaluation=StageEvaluation(result=admission.result)
    )
    stop = next_improvement_stage("fixture", value, (record,), budget, now=NOW)
    assert stop.reason == "result_not_independently_admitted"
    record = record.model_copy(
        update={"evaluation": StageEvaluation(result=admission.result, admission=admission)}
    )
    stop = next_improvement_stage("fixture", value, (record,), budget, now=NOW)
    assert stop.reason == "fresh_snapshot_required"
    assert (
        next_improvement_stage(
            "fixture", value, (record,), budget.model_copy(update={"maximum_stages": 1}), now=NOW
        ).reason
        == "stage_budget_exhausted"
    )
    assert (
        next_improvement_stage(
            "fixture", value, (record,), budget.model_copy(update={"maximum_cost": 1}), now=NOW
        ).reason
        == "cost_budget_exhausted"
    )
