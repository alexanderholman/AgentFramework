from __future__ import annotations

from datetime import UTC, datetime, timedelta

from af.framework.self_operation import (
    AuthorityRecord,
    SelfOperationSnapshot,
    SelfOperationStage,
    StageBudget,
    StageHandoff,
)
from af.framework.self_stages import (
    PriorityCandidate,
    StageEvidenceSnapshot,
    WorkItem,
    evaluate_stage,
)

NOW = datetime(2026, 9, 26, tzinfo=UTC)
D = "sha256:" + "a" * 64


def snapshot(stage, **values):
    source = SelfOperationSnapshot(
        "fixture",
        "owner/repo",
        "main",
        "a" * 40,
        NOW,
        (D,),
        (AuthorityRecord("entry", f"self-{stage.value}", True),),
        (StageBudget("steps", "count", 10, 0),),
    )
    return StageEvidenceSnapshot(
        source=source,
        handoff=StageHandoff(stage, source.digest, D, D, None, "entry"),
        evaluated_at=NOW,
        policy_digest=D,
        objective_id="objective",
        acceptance_clauses=("acceptance-1",),
        **values,
    )


def test_prioritisation_uses_actual_bounded_scores_and_fails_closed_on_ties():
    stage = SelfOperationStage.PRIORITISATION
    first = PriorityCandidate(
        candidate_id="first", objective_id="objective", value=10, aligned=True, evidence_digest=D
    )
    second = first.model_copy(update={"candidate_id": "second", "value": 20})
    value = snapshot(stage, candidates=(first, second))
    result = evaluate_stage(value)
    assert result.artifact["selected"] == "second"
    reversed_result = evaluate_stage(value.model_copy(update={"candidates": (second, first)}))
    assert result.canonical_json() == reversed_result.canonical_json()
    tied = evaluate_stage(
        value.model_copy(update={"candidates": (first, second.model_copy(update={"value": 10}))})
    )
    assert tied.plan.result.reason.value == "ambiguous_selection"
    assert tied.artifact is None


def test_containment_keeps_required_coverage_and_separates_adjacent_work():
    necessary = WorkItem(
        item_id="needed",
        objective_id="objective",
        description="Implement accepted feature",
        acceptance_ids=("acceptance-1",),
    )
    unrelated = WorkItem(item_id="extra", objective_id="another", description="New feature")
    value = snapshot(SelfOperationStage.CONTAINMENT, work_items=(necessary, unrelated))
    result = evaluate_stage(value)
    assert result.artifact["retained"] == ["needed"]
    assert result.artifact["adjacent"] == ["extra"]
    blocked = evaluate_stage(value.model_copy(update={"work_items": (unrelated,)}))
    assert blocked.plan.result.reason.value == "missing_evidence"
    assert blocked.artifact is None


def gates(stage, names):
    from af.framework.self_stages import GateEvidence

    return tuple(
        GateEvidence(
            name=name,
            subject_digest=D,
            evidence_digest=D,
            passed=True,
            reviewer="independent-review",
            independent=True,
            expires_at=NOW + timedelta(hours=1),
        )
        for name in names
    )


def test_assurance_rejects_failed_self_review_stale_and_missing_gates():
    stage = SelfOperationStage.ASSURANCE
    names = ("tests", "security", "compatibility", "provenance", "rollback", "governance")
    value = snapshot(stage, gates=gates(stage, names), implementation_owner="developer")
    assert evaluate_stage(value).plan.result.reason.value == "selected"
    for update, reason in [
        ({"passed": False}, "policy_denied"),
        ({"independent": False}, "policy_denied"),
        ({"reviewer": "developer"}, "policy_denied"),
        ({"expires_at": NOW}, "missing_evidence"),
        ({"subject_digest": "sha256:" + "b" * 64}, "missing_evidence"),
    ]:
        modified = (value.gates[0].model_copy(update=update), *value.gates[1:])
        result = evaluate_stage(value.model_copy(update={"gates": modified}))
        assert result.plan.result.reason.value == reason
        assert result.artifact is None
    assert (
        evaluate_stage(value.model_copy(update={"gates": ()})).plan.result.reason.value
        == "missing_evidence"
    )


def test_specification_coverage_and_dependency_cycles_are_checked():
    item = WorkItem(
        item_id="needed",
        objective_id="objective",
        description="Implementation",
        acceptance_ids=("acceptance-1",),
    )
    value = snapshot(
        SelfOperationStage.SPECIFICATION,
        work_items=(item,),
        invariants=("Preserve accepted semantics",),
        non_goals=("No remote inference",),
    )
    result = evaluate_stage(value)
    assert result.artifact["requirements"][0]["item_id"] == "needed"
    cyclic = item.model_copy(update={"dependencies": ("needed",)})
    result = evaluate_stage(value.model_copy(update={"work_items": (cyclic,)}))
    assert result.plan.result.reason.value == "missing_evidence"


def test_qualification_records_environment_but_grants_no_merge_or_release_authority():
    stage = SelfOperationStage.QUALIFICATION
    names = (
        "tests",
        "security",
        "compatibility",
        "provenance",
        "rollback",
        "governance",
        "environment",
    )
    value = snapshot(
        stage, gates=gates(stage, names), environment="linux-x86_64", qualification_tier="local"
    )
    result = evaluate_stage(value)
    assert result.artifact["environment"] == "linux-x86_64"
    assert result.artifact["grants_authority"] is False
    assert result.plan.allowed_writes == ()


def test_optimisation_requires_semantics_and_uncertainty_separated_benchmark_gain():
    from af.framework.self_stages import MetricEvidence

    stage = SelfOperationStage.OPTIMISATION
    metric = MetricEvidence(
        name="latency",
        baseline=20,
        candidate=10,
        uncertainty=1,
        direction="minimize",
        methodology="Repeated held-out workload",
        evidence_digest=D,
    )
    value = snapshot(
        stage,
        gates=gates(stage, ("semantic-equivalence", "benchmark")),
        accepted_contract_digest=D,
        candidate_contract_digest=D,
        metrics=(metric,),
    )
    result = evaluate_stage(value)
    assert result.artifact["gains"][0]["gain_lower"] == 8
    uncertain = metric.model_copy(update={"uncertainty": 6})
    assert (
        evaluate_stage(value.model_copy(update={"metrics": (uncertain,)})).plan.result.reason.value
        == "no_eligible_input"
    )
    assert (
        evaluate_stage(
            value.model_copy(update={"candidate_contract_digest": None})
        ).plan.result.reason.value
        == "policy_denied"
    )


def test_predecessor_admission_is_exact_current_and_immediately_prior_stage():
    from dataclasses import replace

    from af.framework.self_improvement import AdmittedStage
    from af.framework.self_operation import StageReason, StageResult

    value = snapshot(
        SelfOperationStage.PRIORITISATION,
        candidates=(
            PriorityCandidate(
                candidate_id="candidate",
                objective_id="objective",
                value=10,
                aligned=True,
                evidence_digest=D,
            ),
        ),
    )
    previous_handoff = replace(value.handoff, stage=SelfOperationStage.ALIGNMENT)
    previous_result = StageResult(
        SelfOperationStage.ALIGNMENT, previous_handoff.digest, StageReason.SELECTED, D
    )
    admission = AdmittedStage(
        handoff=previous_handoff,
        result=previous_result,
        reviewer="review",
        independent=True,
        expires_at=NOW + timedelta(hours=1),
    )
    handoff = replace(
        value.handoff, predecessor_result_digest=previous_result.digest, entry_authority_id=None
    )
    value = value.model_copy(update={"handoff": handoff, "predecessor": admission})
    assert evaluate_stage(value).artifact["selected"] == "candidate"
    assert (
        evaluate_stage(value.model_copy(update={"predecessor": None})).plan.result.reason.value
        == "missing_evidence"
    )
    expired = admission.model_copy(update={"expires_at": NOW})
    assert evaluate_stage(value.model_copy(update={"predecessor": expired})).artifact is None


def test_stale_policy_authority_and_budget_are_not_stage_authority():
    from dataclasses import replace

    value = snapshot(SelfOperationStage.PRIORITISATION)
    cases = [
        (value.model_copy(update={"evaluated_at": NOW + timedelta(hours=1)}), "stale_snapshot"),
        (value.model_copy(update={"policy_digest": "sha256:" + "b" * 64}), "policy_denied"),
    ]
    for source_update, reason in [
        ({"authorities": ()}, "missing_authority"),
        ({"budgets": ()}, "no_eligible_input"),
    ]:
        source = replace(value.source, **source_update)
        changed = value.model_copy(
            update={
                "source": source,
                "handoff": replace(value.handoff, source_snapshot_digest=source.digest),
            }
        )
        cases.append((changed, reason))
    for changed, reason in cases:
        result = evaluate_stage(changed)
        assert result.plan.result.reason.value == reason
        assert result.artifact is None


def test_required_dependency_and_stage_evidence_gaps_stop():
    item = WorkItem(
        item_id="needed",
        objective_id="objective",
        description="Implementation",
        acceptance_ids=("acceptance-1",),
        dependencies=("helper",),
    )
    helper = WorkItem(item_id="helper", objective_id="objective", description="Required helper")
    value = snapshot(SelfOperationStage.CONTAINMENT, work_items=(item, helper))
    assert evaluate_stage(value).artifact["retained"] == ["helper", "needed"]
    assert evaluate_stage(value.model_copy(update={"work_items": (item,)})).artifact is None
    assert (
        evaluate_stage(
            snapshot(
                SelfOperationStage.SPECIFICATION,
                work_items=(item.model_copy(update={"dependencies": ()}),),
            )
        ).artifact
        is None
    )
    assert evaluate_stage(snapshot(SelfOperationStage.QUALIFICATION)).artifact is None
    assert evaluate_stage(snapshot(SelfOperationStage.ANALYSIS)).artifact is None
    assert evaluate_stage(snapshot(SelfOperationStage.PRIORITISATION)).artifact is None


def test_optimisation_rejects_absent_or_unbound_measurements():
    from af.framework.self_stages import MetricEvidence

    stage = SelfOperationStage.OPTIMISATION
    value = snapshot(stage, accepted_contract_digest=D, candidate_contract_digest=D)
    assert evaluate_stage(value).artifact is None
    value = value.model_copy(update={"gates": gates(stage, ("semantic-equivalence", "benchmark"))})
    assert evaluate_stage(value).artifact is None
    unbound = MetricEvidence(
        name="latency",
        baseline=10,
        candidate=5,
        uncertainty=1,
        direction="minimize",
        methodology="fixed workload",
        evidence_digest="sha256:" + "b" * 64,
    )
    assert evaluate_stage(value.model_copy(update={"metrics": (unbound,)})).artifact is None


def test_stage_input_rejects_duplicate_and_naive_evidence():
    import pytest

    from af.framework.self_stages import GateEvidence

    with pytest.raises(ValueError, match="unique"):
        snapshot(
            SelfOperationStage.PRIORITISATION,
            candidates=(
                PriorityCandidate(candidate_id="same", objective_id="objective", evidence_digest=D),
            )
            * 2,
        )
    value = snapshot(SelfOperationStage.PRIORITISATION)
    with pytest.raises(ValueError, match="timezone"):
        StageEvidenceSnapshot.model_validate(
            {**value.model_dump(), "evaluated_at": NOW.replace(tzinfo=None)}
        )
    with pytest.raises(ValueError, match="timezone"):
        GateEvidence(
            name="test",
            subject_digest=D,
            evidence_digest=D,
            passed=True,
            reviewer="reviewer",
            expires_at=NOW.replace(tzinfo=None),
        )


def test_real_stage_adapter_requires_external_review_and_exact_dispatch_binding():
    from dataclasses import replace

    import pytest

    from af.application.self_stages import EvidenceSelfImprovementStagePort, SelfStageController
    from af.framework.self_improvement import AdmittedStage, ImprovementSnapshot, StageDispatch

    value = snapshot(
        SelfOperationStage.PRIORITISATION,
        candidates=(
            PriorityCandidate(
                candidate_id="candidate",
                objective_id="objective",
                value=10,
                aligned=True,
                evidence_digest=D,
            ),
        ),
    )
    improvement = ImprovementSnapshot(
        source=value.source,
        handoffs=tuple(replace(value.handoff, stage=stage) for stage in SelfOperationStage),
    )
    dispatch = StageDispatch(
        operation_id="operation",
        stage=value.handoff.stage,
        handoff=value.handoff,
        snapshot_digest=improvement.digest,
        effect_key="claim",
    )

    class Source:
        def load(self, dispatch, snapshot):
            return value

    class Review:
        def admit(self, evaluation):
            return AdmittedStage(
                handoff=evaluation.plan.handoff,
                result=evaluation.plan.result,
                independent=True,
                reviewer="external-reviewer",
                expires_at=NOW + timedelta(hours=1),
            )

    result = EvidenceSelfImprovementStagePort(Source()).invoke(dispatch, improvement)
    assert result.result.output_digest is not None
    assert result.admission is None
    result = EvidenceSelfImprovementStagePort(Source(), Review()).invoke(dispatch, improvement)
    assert result.admission.result == result.result
    with pytest.raises(ValueError, match="exact snapshot"):
        EvidenceSelfImprovementStagePort(Source()).invoke(
            dispatch.model_copy(update={"snapshot_digest": D}), improvement
        )
    assert (
        SelfStageController(lambda stage: value).evaluate(value.handoff.stage).artifact["selected"]
        == "candidate"
    )
