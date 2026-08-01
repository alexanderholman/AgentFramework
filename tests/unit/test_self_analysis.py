from __future__ import annotations

import unittest
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from af.framework.self_analysis import (
    AnalysisAssessment,
    AnalysisCandidate,
    AnalysisFocusKind,
    AnalysisOutput,
    AnalysisPlan,
    AnalysisPolicy,
    AnalysisRoute,
    AnalysisSnapshot,
    SelfAnalysisPlanner,
)
from af.framework.self_analysis_operation import SelfAnalysisOperation
from af.framework.self_operation import (
    AuthorityRecord,
    SelfOperationSnapshot,
    SelfOperationStage,
    StageBudget,
    StageHandoff,
    StagePlan,
    StageReason,
    StageResult,
)

CAPTURED_AT = datetime(2026, 7, 24, 19, 30, tzinfo=UTC)
NOW = CAPTURED_AT + timedelta(minutes=1)
HEAD_SHA = "a" * 40
REVISION = "sha256:" + "b" * 64


def candidate(
    candidate_id: str,
    *,
    priority: int = 1,
    urgency: int = 1,
    risk: int = 1,
    age_minutes: int = 1,
    facts: tuple[str, ...] = ("Issue is open and ready.",),
    hypotheses: tuple[str, ...] = ("A shared stage protocol is missing.",),
    uncertainties: tuple[str, ...] = ("No competing implementation exists.",),
    causal_claims: tuple[str, ...] = ("Shared contracts unblock later stages.",),
    clarification_needs: tuple[str, ...] = ("Confirm the smallest accepted slice.",),
    active_overlap: bool = False,
    evidence_complete: bool = True,
    authority_granted: bool = True,
    complete: bool = False,
) -> AnalysisCandidate:
    return AnalysisCandidate(
        candidate_id=candidate_id,
        revision=REVISION,
        focus_kind=AnalysisFocusKind.GAP,
        priority=priority,
        urgency=urgency,
        risk=risk,
        created_at=CAPTURED_AT - timedelta(minutes=age_minutes),
        facts=facts,
        hypotheses=hypotheses,
        uncertainties=uncertainties,
        causal_claims=causal_claims,
        clarification_needs=clarification_needs,
        active_overlap=active_overlap,
        evidence_complete=evidence_complete,
        authority_granted=authority_granted,
        complete=complete,
    )


def operation_snapshot(*, authority_granted: bool = True) -> SelfOperationSnapshot:
    return SelfOperationSnapshot(
        source_id="github",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha=HEAD_SHA,
        captured_at=CAPTURED_AT,
        evidence_digests=("sha256:" + "c" * 64,),
        authorities=(
            AuthorityRecord(
                authority_id="authority:entry-point",
                scope_id="scope:read-only-analysis",
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


def snapshot(*candidates: AnalysisCandidate, authority_granted: bool = True) -> AnalysisSnapshot:
    return AnalysisSnapshot(
        operation=operation_snapshot(authority_granted=authority_granted),
        candidates=candidates,
    )


def policy(*, tie_break_by_id: bool = False) -> AnalysisPolicy:
    return AnalysisPolicy(
        max_snapshot_age_seconds=300,
        tie_break_by_id=tie_break_by_id,
        entry_authority_id="authority:entry-point",
    )


class SelfAnalysisPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = SelfAnalysisPlanner()

    def test_identical_canonical_evidence_produces_byte_identical_plan(self) -> None:
        observed = snapshot(candidate("issue:126"))

        first = self.planner.plan(observed, policy(), now=NOW)
        second = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.route, AnalysisRoute.ANALYSE)
        self.assertEqual(first.stage_plan.stage, SelfOperationStage.ANALYSIS)
        self.assertEqual(first.selected_candidate_id, "issue:126")
        assert first.output is not None
        self.assertEqual(first.output.facts, ("Issue is open and ready.",))
        self.assertEqual(first.stage_plan.allowed_writes, ())
        self.assertEqual(first.stage_plan.handoff.entry_authority_id, "authority:entry-point")
        self.assertEqual(first.stage_plan.handoff.source_snapshot_digest, observed.operation.digest)
        self.assertEqual(first.stage_plan.handoff.input_artifact_digest, observed.digest)

    def test_input_order_does_not_change_snapshot_or_selection(self) -> None:
        preferred = candidate("issue:126", priority=0)
        later = candidate("issue:125", priority=1)

        forward = snapshot(preferred, later)
        reverse = snapshot(later, preferred)

        self.assertEqual(forward.digest, reverse.digest)
        self.assertEqual(
            self.planner.plan(forward, policy(), now=NOW).canonical_json(),
            self.planner.plan(reverse, policy(), now=NOW).canonical_json(),
        )

    def test_active_overlap_stops_without_writes(self) -> None:
        stopped = self.planner.plan(
            snapshot(candidate("issue:126", active_overlap=True)),
            policy(),
            now=NOW,
        )

        self.assertEqual(stopped.route, AnalysisRoute.STOP)
        self.assertEqual(stopped.stage_plan.result.reason, StageReason.NO_ELIGIBLE_INPUT)
        self.assertEqual(stopped.assessments[0].reason, StageReason.ACTIVE_OVERLAP)
        self.assertEqual(stopped.stage_plan.allowed_writes, ())

    def test_missing_authority_evidence_and_complete_have_typed_reasons(self) -> None:
        observed = snapshot(
            candidate("issue:authority", authority_granted=False),
            candidate("issue:evidence", evidence_complete=False),
            candidate("issue:complete", complete=True),
        )

        stopped = self.planner.plan(observed, policy(), now=NOW)
        reasons = {item.candidate_id: item.reason for item in stopped.assessments}

        self.assertEqual(
            reasons,
            {
                "issue:authority": StageReason.MISSING_AUTHORITY,
                "issue:complete": StageReason.COMPLETE,
                "issue:evidence": StageReason.MISSING_EVIDENCE,
            },
        )

    def test_missing_entry_authority_stops_every_candidate(self) -> None:
        observed = snapshot(candidate("issue:126"), authority_granted=False)

        stopped = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(stopped.route, AnalysisRoute.STOP)
        self.assertEqual(stopped.stage_plan.result.reason, StageReason.MISSING_AUTHORITY)
        self.assertEqual(
            {item.reason for item in stopped.assessments},
            {StageReason.MISSING_AUTHORITY},
        )

    def test_exact_tie_fails_closed_without_explicit_tie_break(self) -> None:
        observed = snapshot(candidate("issue:125"), candidate("issue:126"))

        stopped = self.planner.plan(observed, policy(), now=NOW)
        selected = self.planner.plan(
            observed,
            policy(tie_break_by_id=True),
            now=NOW,
        )

        self.assertEqual(stopped.route, AnalysisRoute.STOP)
        self.assertEqual(stopped.stage_plan.result.reason, StageReason.AMBIGUOUS_SELECTION)
        self.assertEqual(selected.selected_candidate_id, "issue:125")

    def test_priority_urgency_risk_and_age_order_selection(self) -> None:
        observed = snapshot(
            candidate("issue:priority", priority=0, risk=9),
            candidate("issue:urgency", priority=1, urgency=9, risk=9),
            candidate("issue:risk", priority=1, urgency=9, risk=1),
            candidate("issue:oldest", priority=1, urgency=9, risk=1, age_minutes=10),
        )

        selected = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(selected.selected_candidate_id, "issue:priority")

    def test_stale_snapshot_stops_before_selection(self) -> None:
        stopped = self.planner.plan(
            snapshot(candidate("issue:126")),
            policy(),
            now=CAPTURED_AT + timedelta(seconds=301),
        )

        self.assertEqual(stopped.route, AnalysisRoute.STOP)
        self.assertEqual(stopped.stage_plan.result.reason, StageReason.STALE_SNAPSHOT)
        self.assertEqual(
            {item.reason for item in stopped.assessments},
            {StageReason.STALE_SNAPSHOT},
        )

    def test_snapshot_and_candidate_bounds_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "candidate identities must be unique"):
            snapshot(candidate("issue:126"), candidate("issue:126"))
        with self.assertRaisesRegex(ValueError, "facts"):
            replace(candidate("issue:126"), facts=())
        with self.assertRaisesRegex(ValueError, "entry authority"):
            replace(policy(), entry_authority_id="")

    def test_planning_time_must_not_precede_observation(self) -> None:
        with self.assertRaisesRegex(ValueError, "precedes snapshot"):
            self.planner.plan(
                snapshot(candidate("issue:126")),
                policy(),
                now=CAPTURED_AT - timedelta(microseconds=1),
            )

    def test_plan_type_remains_read_only(self) -> None:
        plan = self.planner.plan(snapshot(candidate("issue:126")), policy(), now=NOW)

        self.assertIsInstance(plan, AnalysisPlan)
        with self.assertRaisesRegex(ValueError, "cannot grant writes"):
            replace(plan.stage_plan, allowed_writes=("git:push",))

    def test_candidate_and_output_validation_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "focus kind"):
            replace(candidate("issue:126"), focus_kind=cast(AnalysisFocusKind, object()))
        with self.assertRaisesRegex(ValueError, "eligibility facts"):
            replace(candidate("issue:126"), active_overlap=cast(bool, 1))
        with self.assertRaisesRegex(ValueError, "clarification needs"):
            replace(candidate("issue:126"), clarification_needs=("duplicate", "duplicate"))
        with self.assertRaisesRegex(ValueError, "operation snapshot"):
            AnalysisSnapshot(
                operation=cast(SelfOperationSnapshot, object()),
                candidates=(candidate("issue:126"),),
            )
        with self.assertRaisesRegex(ValueError, "candidates are invalid"):
            AnalysisSnapshot(
                operation=operation_snapshot(),
                candidates=cast(tuple[AnalysisCandidate, ...], (object(),)),
            )
        with self.assertRaisesRegex(ValueError, "assessment identity"):
            AnalysisAssessment(candidate_id="", reason=StageReason.SELECTED)
        with self.assertRaisesRegex(ValueError, "output focus kind"):
            AnalysisOutput(
                focus_kind=cast(AnalysisFocusKind, object()),
                facts=("fact",),
                hypotheses=("hypothesis",),
                uncertainties=("uncertainty",),
                causal_claims=("claim",),
                clarification_needs=("need",),
            )

    def test_analysis_plan_validation_fail_closed(self) -> None:
        observed = snapshot(candidate("issue:126"))
        handoff = StageHandoff(
            stage=SelfOperationStage.ANALYSIS,
            source_snapshot_digest=observed.operation.digest,
            input_artifact_digest=observed.digest,
            policy_digest=policy().digest,
            predecessor_result_digest=None,
            entry_authority_id="authority:entry-point",
        )
        output = AnalysisOutput(
            focus_kind=AnalysisFocusKind.GAP,
            facts=("fact",),
            hypotheses=("hypothesis",),
            uncertainties=("uncertainty",),
            causal_claims=("claim",),
            clarification_needs=("need",),
        )
        stage_plan = StagePlan(
            stage=SelfOperationStage.ANALYSIS,
            handoff=handoff,
            result=StageResult(
                stage=SelfOperationStage.ANALYSIS,
                handoff_digest=handoff.digest,
                reason=StageReason.SELECTED,
                output_digest=output.digest,
            ),
            route="analyse",
            allowed_writes=(),
            stop_conditions=("authority_missing", "snapshot_stale", "unsafe"),
        )
        with self.assertRaisesRegex(ValueError, "stage plan is invalid"):
            AnalysisPlan(
                stage_plan=cast(StagePlan, object()),
                route=AnalysisRoute.ANALYSE,
                selected_candidate_id="issue:126",
                focus_kind=AnalysisFocusKind.GAP,
                output=output,
                assessments=(AnalysisAssessment("issue:126", StageReason.SELECTED),),
            )
        with self.assertRaisesRegex(ValueError, "active plan is inconsistent"):
            AnalysisPlan(
                stage_plan=replace(
                    stage_plan,
                    result=replace(stage_plan.result, output_digest=REVISION),
                ),
                route=AnalysisRoute.ANALYSE,
                selected_candidate_id="issue:126",
                focus_kind=AnalysisFocusKind.GAP,
                output=output,
                assessments=(AnalysisAssessment("issue:126", StageReason.SELECTED),),
            )
        with self.assertRaisesRegex(ValueError, "stop plan is inconsistent"):
            AnalysisPlan(
                stage_plan=StagePlan(
                    stage=SelfOperationStage.ANALYSIS,
                    handoff=handoff,
                    result=StageResult(
                        stage=SelfOperationStage.ANALYSIS,
                        handoff_digest=handoff.digest,
                        reason=StageReason.NO_ELIGIBLE_INPUT,
                        output_digest=None,
                    ),
                    route="stop",
                    allowed_writes=(),
                    stop_conditions=("authority_missing", "snapshot_stale", "unsafe"),
                ),
                route=AnalysisRoute.STOP,
                selected_candidate_id="issue:126",
                focus_kind=None,
                output=None,
                assessments=(AnalysisAssessment("issue:126", StageReason.NO_ELIGIBLE_INPUT),),
            )

    def test_operation_boundary_validation_fail_closed(self) -> None:
        class Provider:
            def __init__(self, returned_snapshot: object) -> None:
                self.returned_snapshot = returned_snapshot

            def capture_snapshot(self) -> AnalysisSnapshot:
                return cast(AnalysisSnapshot, self.returned_snapshot)

        valid_provider = Provider(snapshot(candidate("issue:126")))
        with self.assertRaisesRegex(TypeError, "provider is invalid"):
            SelfAnalysisOperation(
                cast(Any, object()),
                policy(),
                lambda: NOW,
            )
        with self.assertRaisesRegex(TypeError, "policy is invalid"):
            SelfAnalysisOperation(valid_provider, cast(AnalysisPolicy, object()), lambda: NOW)
        with self.assertRaisesRegex(TypeError, "clock is invalid"):
            SelfAnalysisOperation(
                valid_provider,
                policy(),
                cast(Callable[[], datetime], object()),
            )
        with self.assertRaisesRegex(TypeError, "planner is invalid"):
            SelfAnalysisOperation(
                valid_provider,
                policy(),
                lambda: NOW,
                planner=cast(Any, object()),
            )
        with self.assertRaisesRegex(TypeError, "returned an invalid snapshot"):
            SelfAnalysisOperation(Provider(object()), policy(), lambda: NOW).prepare()
        with self.assertRaisesRegex(TypeError, "returned an invalid time"):
            SelfAnalysisOperation(
                valid_provider,
                policy(),
                lambda: cast(datetime, object()),
            ).prepare()
        valid_plan = SelfAnalysisOperation(valid_provider, policy(), lambda: NOW).prepare()
        self.assertEqual(
            valid_plan.plan.stage_plan.handoff.input_artifact_digest,
            valid_plan.snapshot.digest,
        )


if __name__ == "__main__":
    unittest.main()
