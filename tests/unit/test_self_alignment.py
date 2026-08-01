from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from af.framework.self_alignment import (
    AlignmentAssessment,
    AlignmentCandidate,
    AlignmentOutput,
    AlignmentPlan,
    AlignmentPolicy,
    AlignmentRoute,
    AlignmentSnapshot,
    SelfAlignmentPlanner,
)
from af.framework.self_alignment_operation import SelfAlignmentOperation
from af.framework.self_analysis import (
    AnalysisCandidate,
    AnalysisFocusKind,
    AnalysisPolicy,
    AnalysisSnapshot,
    SelfAnalysisPlanner,
)
from af.framework.self_operation import (
    AuthorityRecord,
    SelfOperationSnapshot,
    SelfOperationStage,
    StageBudget,
    StageReason,
)

CAPTURED_AT = datetime(2026, 7, 24, 21, 0, tzinfo=UTC)
NOW = CAPTURED_AT + timedelta(minutes=1)
HEAD_SHA = "a" * 40
REVISION = "sha256:" + "b" * 64
INTENT_DIGEST = "sha256:" + "c" * 64
ARCHITECTURE_DIGEST = "sha256:" + "d" * 64


def operation_snapshot(*, authority_granted: bool = True) -> SelfOperationSnapshot:
    return SelfOperationSnapshot(
        source_id="github",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha=HEAD_SHA,
        captured_at=CAPTURED_AT,
        evidence_digests=("sha256:" + "e" * 64,),
        authorities=(
            AuthorityRecord(
                authority_id="authority:entry-point",
                scope_id="scope:read-only-analysis",
                granted=True,
            ),
            AuthorityRecord(
                authority_id="authority:alignment",
                scope_id="scope:read-only-alignment",
                granted=authority_granted,
            ),
        ),
        budgets=(
            StageBudget(
                budget_id="budget:read-only-cycle",
                unit="cycles",
                limit=1,
                used=0,
            ),
        ),
    )


def analysis_snapshot(
    *,
    active_overlap: bool = False,
    operation: SelfOperationSnapshot | None = None,
) -> AnalysisSnapshot:
    return AnalysisSnapshot(
        operation=operation_snapshot() if operation is None else operation,
        candidates=(
            AnalysisCandidate(
                candidate_id="issue:126",
                revision=REVISION,
                focus_kind=AnalysisFocusKind.GAP,
                priority=0,
                urgency=5,
                risk=1,
                created_at=CAPTURED_AT,
                facts=("Issue is ready for alignment.",),
                hypotheses=("The next read-only stage is missing.",),
                uncertainties=("No competing owner exists.",),
                causal_claims=("A shared handoff unblocks later stages.",),
                clarification_needs=("Confirm the smallest missing slice.",),
                active_overlap=active_overlap,
                evidence_complete=True,
                authority_granted=True,
                complete=False,
            ),
        ),
    )


def predecessor_plan(
    *,
    active_overlap: bool = False,
    operation: SelfOperationSnapshot | None = None,
):
    return SelfAnalysisPlanner().plan(
        analysis_snapshot(active_overlap=active_overlap, operation=operation),
        AnalysisPolicy(
            max_snapshot_age_seconds=300,
            tie_break_by_id=True,
            entry_authority_id="authority:entry-point",
            entry_scope_id="scope:read-only-analysis",
        ),
        now=NOW,
    )


def candidate(
    candidate_id: str,
    *,
    priority: int = 1,
    risk: int = 1,
    age_minutes: int = 1,
    objective_id: str = "objective:self-align",
    operator_intent_digest: str = INTENT_DIGEST,
    architecture_digest: str = ARCHITECTURE_DIGEST,
    commitment_ids: tuple[str, ...] = ("commitment:issue-126",),
    constraint_ids: tuple[str, ...] = ("constraint:read-only",),
    unresolved_conflict_ids: tuple[str, ...] = (),
    active_overlap: bool = False,
    intent_preserved: bool = True,
    policy_consistent: bool = True,
    architecture_consistent: bool = True,
    commitments_consistent: bool = True,
    authority_granted: bool = True,
    evidence_complete: bool = True,
    complete: bool = False,
) -> AlignmentCandidate:
    return AlignmentCandidate(
        candidate_id=candidate_id,
        revision=REVISION,
        objective_id=objective_id,
        priority=priority,
        risk=risk,
        created_at=CAPTURED_AT - timedelta(minutes=age_minutes),
        operator_intent_digest=operator_intent_digest,
        architecture_digest=architecture_digest,
        commitment_ids=commitment_ids,
        constraint_ids=constraint_ids,
        unresolved_conflict_ids=unresolved_conflict_ids,
        active_overlap=active_overlap,
        intent_preserved=intent_preserved,
        policy_consistent=policy_consistent,
        architecture_consistent=architecture_consistent,
        commitments_consistent=commitments_consistent,
        authority_granted=authority_granted,
        evidence_complete=evidence_complete,
        complete=complete,
    )


def snapshot(
    *candidates: AlignmentCandidate,
    authority_granted: bool = True,
    prior_plan=None,
) -> AlignmentSnapshot:
    operation = operation_snapshot(authority_granted=authority_granted)
    return AlignmentSnapshot(
        operation=operation,
        predecessor_plan=(
            predecessor_plan(operation=operation) if prior_plan is None else prior_plan
        ),
        candidates=candidates,
    )


def policy(*, tie_break_by_id: bool = False) -> AlignmentPolicy:
    return AlignmentPolicy(
        max_snapshot_age_seconds=300,
        tie_break_by_id=tie_break_by_id,
        required_authority_id="authority:alignment",
        required_scope_id="scope:read-only-alignment",
    )


class SelfAlignmentPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = SelfAlignmentPlanner()

    def test_identical_canonical_evidence_produces_byte_identical_plan(self) -> None:
        observed = snapshot(candidate("alignment:126"))

        first = self.planner.plan(observed, policy(), now=NOW)
        second = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.route, AlignmentRoute.ALIGN)
        self.assertEqual(first.stage_plan.stage, SelfOperationStage.ALIGNMENT)
        self.assertEqual(first.selected_candidate_id, "alignment:126")
        assert first.output is not None
        self.assertEqual(first.output.objective_id, "objective:self-align")
        self.assertEqual(first.stage_plan.allowed_writes, ())
        self.assertIsNone(first.stage_plan.handoff.entry_authority_id)
        self.assertEqual(
            first.stage_plan.handoff.predecessor_result_digest,
            observed.predecessor_plan.stage_plan.result.digest,
        )
        self.assertEqual(first.stage_plan.handoff.source_snapshot_digest, observed.operation.digest)
        self.assertEqual(first.stage_plan.handoff.input_artifact_digest, observed.digest)

    def test_input_order_does_not_change_snapshot_or_selection(self) -> None:
        preferred = candidate("alignment:126", priority=0)
        later = candidate("alignment:125", priority=1)

        forward = snapshot(preferred, later)
        reverse = snapshot(later, preferred)

        self.assertEqual(forward.digest, reverse.digest)
        self.assertEqual(
            self.planner.plan(forward, policy(), now=NOW).canonical_json(),
            self.planner.plan(reverse, policy(), now=NOW).canonical_json(),
        )

    def test_active_overlap_and_unresolved_conflicts_have_typed_reasons(self) -> None:
        stopped = self.planner.plan(
            snapshot(
                candidate("alignment:overlap", active_overlap=True),
                candidate("alignment:unsafe", unresolved_conflict_ids=("conflict:open",)),
            ),
            policy(),
            now=NOW,
        )

        self.assertEqual(stopped.route, AlignmentRoute.STOP)
        self.assertEqual(stopped.stage_plan.result.reason, StageReason.NO_ELIGIBLE_INPUT)
        reasons = {item.candidate_id: item.reason for item in stopped.assessments}
        self.assertEqual(reasons["alignment:overlap"], StageReason.ACTIVE_OVERLAP)
        self.assertEqual(reasons["alignment:unsafe"], StageReason.UNSAFE)
        self.assertEqual(stopped.stage_plan.allowed_writes, ())

    def test_missing_authority_stale_snapshot_and_predecessor_output_stop(self) -> None:
        missing_authority = self.planner.plan(
            snapshot(candidate("alignment:126"), authority_granted=False),
            policy(),
            now=NOW,
        )
        stale = self.planner.plan(
            snapshot(candidate("alignment:126")),
            policy(),
            now=CAPTURED_AT + timedelta(seconds=301),
        )
        missing_predecessor = self.planner.plan(
            snapshot(
                candidate("alignment:126"),
                prior_plan=predecessor_plan(active_overlap=True),
            ),
            policy(),
            now=NOW,
        )

        self.assertEqual(missing_authority.route, AlignmentRoute.STOP)
        self.assertEqual(missing_authority.stage_plan.result.reason, StageReason.MISSING_AUTHORITY)
        self.assertEqual(
            {item.reason for item in missing_authority.assessments},
            {StageReason.MISSING_AUTHORITY},
        )
        self.assertEqual(stale.route, AlignmentRoute.STOP)
        self.assertEqual(stale.stage_plan.result.reason, StageReason.STALE_SNAPSHOT)
        self.assertEqual(missing_predecessor.route, AlignmentRoute.STOP)
        self.assertEqual(missing_predecessor.stage_plan.result.reason, StageReason.MISSING_EVIDENCE)
        self.assertEqual(
            {item.reason for item in missing_predecessor.assessments},
            {StageReason.MISSING_EVIDENCE},
        )

    def test_predecessor_source_snapshot_must_match_operation_snapshot(self) -> None:
        prior_plan = predecessor_plan()
        mismatched = AlignmentSnapshot(
            operation=replace(
                operation_snapshot(),
                evidence_digests=("sha256:" + "f" * 64,),
            ),
            predecessor_plan=prior_plan,
            candidates=(candidate("alignment:126"),),
        )

        stopped = self.planner.plan(mismatched, policy(), now=NOW)

        self.assertEqual(stopped.route, AlignmentRoute.STOP)
        self.assertEqual(stopped.stage_plan.result.reason, StageReason.MISSING_EVIDENCE)
        self.assertEqual(
            {item.reason for item in stopped.assessments},
            {StageReason.MISSING_EVIDENCE},
        )

    def test_exact_tie_fails_closed_without_explicit_tie_break(self) -> None:
        observed = snapshot(candidate("alignment:125"), candidate("alignment:126"))

        stopped = self.planner.plan(observed, policy(), now=NOW)
        selected = self.planner.plan(observed, policy(tie_break_by_id=True), now=NOW)

        self.assertEqual(stopped.route, AlignmentRoute.STOP)
        self.assertEqual(stopped.stage_plan.result.reason, StageReason.AMBIGUOUS_SELECTION)
        self.assertEqual({item.reason for item in stopped.assessments}, {StageReason.ELIGIBLE})
        self.assertEqual(selected.selected_candidate_id, "alignment:125")
        selected_reasons = {item.candidate_id: item.reason for item in selected.assessments}
        self.assertEqual(selected_reasons["alignment:125"], StageReason.SELECTED)
        self.assertEqual(selected_reasons["alignment:126"], StageReason.ELIGIBLE)

    def test_policy_and_commitment_preservation_fail_closed(self) -> None:
        observed = snapshot(
            candidate("alignment:intent", intent_preserved=False),
            candidate("alignment:policy", policy_consistent=False),
            candidate("alignment:commitments", commitments_consistent=False),
        )

        stopped = self.planner.plan(observed, policy(), now=NOW)
        reasons = {item.candidate_id: item.reason for item in stopped.assessments}

        self.assertEqual(
            reasons,
            {
                "alignment:commitments": StageReason.POLICY_DENIED,
                "alignment:intent": StageReason.POLICY_DENIED,
                "alignment:policy": StageReason.POLICY_DENIED,
            },
        )

    def test_snapshot_and_candidate_bounds_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "candidate identities must be unique"):
            snapshot(candidate("alignment:126"), candidate("alignment:126"))
        with self.assertRaisesRegex(ValueError, "objective identity"):
            replace(candidate("alignment:126"), objective_id="")
        with self.assertRaisesRegex(ValueError, "constraint identities"):
            replace(candidate("alignment:126"), constraint_ids=("constraint:dup", "constraint:dup"))
        with self.assertRaisesRegex(ValueError, "required authority"):
            replace(policy(), required_authority_id="")
        with self.assertRaisesRegex(ValueError, "required scope"):
            replace(policy(), required_scope_id="")

    def test_planning_time_must_not_precede_observation(self) -> None:
        with self.assertRaisesRegex(ValueError, "precedes snapshot"):
            self.planner.plan(
                snapshot(candidate("alignment:126")),
                policy(),
                now=CAPTURED_AT - timedelta(microseconds=1),
            )

    def test_plan_type_remains_read_only(self) -> None:
        plan = self.planner.plan(snapshot(candidate("alignment:126")), policy(), now=NOW)

        self.assertIsInstance(plan, AlignmentPlan)
        with self.assertRaisesRegex(ValueError, "cannot grant writes"):
            replace(plan.stage_plan, allowed_writes=("git:push",))

    def test_candidate_output_and_plan_validation_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "eligibility facts"):
            replace(candidate("alignment:126"), active_overlap=cast(bool, 1))
        with self.assertRaisesRegex(ValueError, "output objective identity"):
            AlignmentOutput(
                objective_id="",
                operator_intent_digest=INTENT_DIGEST,
                architecture_digest=ARCHITECTURE_DIGEST,
                commitment_ids=("commitment:1",),
                constraint_ids=("constraint:1",),
            )
        with self.assertRaisesRegex(ValueError, "predecessor plan"):
            AlignmentSnapshot(
                operation=operation_snapshot(),
                predecessor_plan=cast(Any, object()),
                candidates=(candidate("alignment:126"),),
            )

        observed = snapshot(candidate("alignment:126"))
        plan = self.planner.plan(observed, policy(), now=NOW)
        assert plan.output is not None
        with self.assertRaisesRegex(ValueError, "active plan is inconsistent"):
            AlignmentPlan(
                stage_plan=replace(
                    plan.stage_plan,
                    result=replace(plan.stage_plan.result, output_digest=REVISION),
                ),
                route=AlignmentRoute.ALIGN,
                selected_candidate_id="alignment:126",
                objective_id=plan.output.objective_id,
                output=plan.output,
                assessments=plan.assessments,
            )
        with self.assertRaisesRegex(ValueError, "stop plan is inconsistent"):
            AlignmentPlan(
                stage_plan=replace(
                    plan.stage_plan,
                    route="stop",
                    result=replace(
                        plan.stage_plan.result,
                        reason=StageReason.NO_ELIGIBLE_INPUT,
                        output_digest=None,
                    ),
                ),
                route=AlignmentRoute.STOP,
                selected_candidate_id="alignment:126",
                objective_id=None,
                output=None,
                assessments=(AlignmentAssessment("alignment:126", StageReason.NO_ELIGIBLE_INPUT),),
            )

    def test_operation_boundary_validation_fail_closed(self) -> None:
        class Provider:
            def __init__(self, returned_snapshot: object) -> None:
                self.returned_snapshot = returned_snapshot

            def capture_snapshot(self) -> AlignmentSnapshot:
                return cast(AlignmentSnapshot, self.returned_snapshot)

        valid_provider = Provider(snapshot(candidate("alignment:126")))
        with self.assertRaisesRegex(TypeError, "provider is invalid"):
            SelfAlignmentOperation(cast(Any, object()), policy(), lambda: NOW)
        with self.assertRaisesRegex(TypeError, "policy is invalid"):
            SelfAlignmentOperation(valid_provider, cast(AlignmentPolicy, object()), lambda: NOW)
        with self.assertRaisesRegex(TypeError, "clock is invalid"):
            SelfAlignmentOperation(
                valid_provider,
                policy(),
                cast(Callable[[], datetime], object()),
            )
        with self.assertRaisesRegex(TypeError, "planner is invalid"):
            SelfAlignmentOperation(
                valid_provider,
                policy(),
                lambda: NOW,
                planner=cast(Any, object()),
            )
        with self.assertRaisesRegex(TypeError, "returned an invalid snapshot"):
            SelfAlignmentOperation(Provider(object()), policy(), lambda: NOW).prepare()
        with self.assertRaisesRegex(TypeError, "returned an invalid time"):
            SelfAlignmentOperation(
                valid_provider,
                policy(),
                lambda: cast(datetime, object()),
            ).prepare()
        valid_plan = SelfAlignmentOperation(valid_provider, policy(), lambda: NOW).prepare()
        self.assertEqual(
            valid_plan.plan.stage_plan.handoff.input_artifact_digest,
            valid_plan.snapshot.digest,
        )


if __name__ == "__main__":
    unittest.main()
