from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from af.framework.self_design import (
    DesignCandidate,
    DesignPolicy,
    DesignReason,
    DesignRoute,
    DesignSnapshot,
    DesignUnitKind,
    SelfDesignPlanner,
)

CAPTURED_AT = datetime(2026, 7, 24, 18, 30, tzinfo=UTC)
NOW = CAPTURED_AT + timedelta(minutes=1)
HEAD_SHA = "a" * 40
EVIDENCE_DIGEST = "sha256:" + "b" * 64


def candidate(
    candidate_id: str,
    *,
    priority: int = 1,
    blocking_impact: int = 1,
    risk: int = 1,
    age_minutes: int = 1,
    objective_id: str | None = "objective:mvp",
    acceptance_clause_ids: tuple[str, ...] = ("acceptance:verified",),
    dependency_ids: tuple[str, ...] = (),
    active_overlap: bool = False,
    acceptance_complete: bool = True,
    append_only_safe: bool = True,
    authority_granted: bool = True,
    evidence_complete: bool = True,
    complete: bool = False,
) -> DesignCandidate:
    return DesignCandidate(
        candidate_id=candidate_id,
        revision=EVIDENCE_DIGEST,
        kind=DesignUnitKind.CHILD_TASK,
        priority=priority,
        blocking_impact=blocking_impact,
        risk=risk,
        created_at=CAPTURED_AT - timedelta(minutes=age_minutes),
        objective_id=objective_id,
        acceptance_clause_ids=acceptance_clause_ids,
        dependency_ids=dependency_ids,
        active_overlap=active_overlap,
        acceptance_complete=acceptance_complete,
        append_only_safe=append_only_safe,
        authority_granted=authority_granted,
        evidence_complete=evidence_complete,
        complete=complete,
    )


def snapshot(*candidates: DesignCandidate) -> DesignSnapshot:
    return DesignSnapshot(
        source_id="github",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha=HEAD_SHA,
        captured_at=CAPTURED_AT,
        candidates=candidates,
    )


def policy(*, tie_break_by_id: bool = False) -> DesignPolicy:
    return DesignPolicy(
        max_snapshot_age_seconds=300,
        tie_break_by_id=tie_break_by_id,
    )


class SelfDesignPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = SelfDesignPlanner()

    def test_identical_canonical_evidence_produces_byte_identical_plan(self) -> None:
        observed = snapshot(candidate("task:124"))

        first = self.planner.plan(observed, policy(), now=NOW)
        second = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.route, DesignRoute.PROPOSE)
        self.assertEqual(first.selected_candidate_id, "task:124")
        self.assertEqual(first.objective_id, "objective:mvp")
        self.assertEqual(first.acceptance_clause_ids, ("acceptance:verified",))
        self.assertEqual(first.allowed_writes, ())

    def test_input_order_does_not_change_snapshot_or_selection(self) -> None:
        preferred = candidate("task:124", priority=0)
        later = candidate("task:126", priority=1)

        forward = snapshot(preferred, later)
        reverse = snapshot(later, preferred)

        self.assertEqual(forward.digest, reverse.digest)
        self.assertEqual(
            self.planner.plan(forward, policy(), now=NOW).canonical_json(),
            self.planner.plan(reverse, policy(), now=NOW).canonical_json(),
        )

    def test_existing_overlap_blocks_duplicate_design(self) -> None:
        stopped = self.planner.plan(
            snapshot(candidate("task:124", active_overlap=True)),
            policy(),
            now=NOW,
        )

        self.assertEqual(stopped.route, DesignRoute.STOP)
        self.assertEqual(stopped.decision.reason, DesignReason.NO_ELIGIBLE_DESIGN)
        self.assertEqual(
            stopped.decision.assessments[0].reason,
            DesignReason.ACTIVE_OVERLAP,
        )
        self.assertEqual(stopped.allowed_writes, ())

    def test_missing_acceptance_criteria_stops_without_writes(self) -> None:
        stopped = self.planner.plan(
            snapshot(
                candidate(
                    "task:124",
                    acceptance_clause_ids=(),
                    acceptance_complete=False,
                )
            ),
            policy(),
            now=NOW,
        )

        self.assertEqual(
            stopped.decision.assessments[0].reason,
            DesignReason.MISSING_ACCEPTANCE,
        )
        self.assertEqual(stopped.allowed_writes, ())

    def test_dependency_cycle_blocks_each_involved_candidate(self) -> None:
        first = candidate("task:124", dependency_ids=("task:126",))
        second = candidate("task:126", dependency_ids=("task:124",))

        stopped = self.planner.plan(snapshot(first, second), policy(), now=NOW)
        reasons = {item.candidate_id: item.reason for item in stopped.decision.assessments}

        self.assertEqual(
            reasons,
            {
                "task:124": DesignReason.DEPENDENCY_CYCLE,
                "task:126": DesignReason.DEPENDENCY_CYCLE,
            },
        )

    def test_append_only_conflict_blocks_candidate(self) -> None:
        stopped = self.planner.plan(
            snapshot(candidate("spec:history", append_only_safe=False)),
            policy(),
            now=NOW,
        )

        self.assertEqual(
            stopped.decision.assessments[0].reason,
            DesignReason.APPEND_ONLY_CONFLICT,
        )

    def test_stale_head_bound_snapshot_stops_before_selection(self) -> None:
        stopped = self.planner.plan(
            snapshot(candidate("task:124")),
            policy(),
            now=CAPTURED_AT + timedelta(seconds=301),
        )

        self.assertEqual(stopped.route, DesignRoute.STOP)
        self.assertEqual(stopped.decision.reason, DesignReason.STALE_SNAPSHOT)
        self.assertEqual(
            {item.reason for item in stopped.decision.assessments},
            {DesignReason.STALE_SNAPSHOT},
        )

    def test_missing_authority_evidence_or_objective_has_typed_reason(self) -> None:
        observed = snapshot(
            candidate("task:authority", authority_granted=False),
            candidate("task:evidence", evidence_complete=False),
            candidate("task:objective", objective_id=None),
            candidate("task:complete", complete=True),
        )

        stopped = self.planner.plan(observed, policy(), now=NOW)
        reasons = {item.candidate_id: item.reason for item in stopped.decision.assessments}

        self.assertEqual(
            reasons,
            {
                "task:authority": DesignReason.MISSING_AUTHORITY,
                "task:complete": DesignReason.COMPLETE,
                "task:evidence": DesignReason.MISSING_EVIDENCE,
                "task:objective": DesignReason.OBJECTIVE_UNTRACED,
            },
        )
        self.assertEqual(stopped.allowed_writes, ())

    def test_exact_tie_fails_closed_without_explicit_tie_break(self) -> None:
        observed = snapshot(candidate("task:124"), candidate("task:126"))

        stopped = self.planner.plan(observed, policy(), now=NOW)
        selected = self.planner.plan(
            observed,
            policy(tie_break_by_id=True),
            now=NOW,
        )

        self.assertEqual(stopped.decision.reason, DesignReason.AMBIGUOUS_SELECTION)
        self.assertEqual(stopped.route, DesignRoute.STOP)
        self.assertEqual(selected.selected_candidate_id, "task:124")

    def test_priority_impact_risk_and_age_order_selection(self) -> None:
        observed = snapshot(
            candidate("task:priority", priority=0, risk=9),
            candidate("task:impact", priority=1, blocking_impact=9, risk=9),
            candidate("task:risk", priority=1, blocking_impact=9, risk=1),
            candidate(
                "task:oldest",
                priority=1,
                blocking_impact=9,
                risk=1,
                age_minutes=10,
            ),
        )

        selected = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(selected.selected_candidate_id, "task:priority")

    def test_empty_snapshot_returns_typed_stop_plan(self) -> None:
        stopped = self.planner.plan(snapshot(), policy(), now=NOW)

        self.assertEqual(stopped.route, DesignRoute.STOP)
        self.assertEqual(stopped.decision.reason, DesignReason.NO_ELIGIBLE_DESIGN)
        self.assertEqual(stopped.decision.assessments, ())

    def test_read_only_plan_rejects_any_write_collection(self) -> None:
        plan = self.planner.plan(snapshot(candidate("task:124")), policy(), now=NOW)

        with self.assertRaisesRegex(ValueError, "cannot grant writes"):
            replace(plan, allowed_writes=("github:issue",))
        with self.assertRaisesRegex(ValueError, "cannot grant writes"):
            replace(plan, allowed_writes=[])  # type: ignore[arg-type]

    def test_snapshot_and_candidate_bounds_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "candidate identities must be unique"):
            snapshot(candidate("task:124"), candidate("task:124"))
        with self.assertRaisesRegex(ValueError, "candidates are invalid"):
            snapshot(*(candidate(f"task:{index}") for index in range(1_001)))
        with self.assertRaisesRegex(ValueError, "head SHA"):
            replace(snapshot(candidate("task:124")), head_sha="master")
        with self.assertRaisesRegex(ValueError, "revision"):
            replace(candidate("task:124"), revision="unverified")
        with self.assertRaisesRegex(ValueError, "aware"):
            replace(candidate("task:124"), created_at=datetime(2026, 7, 24))
        with self.assertRaisesRegex(ValueError, "dependency identities"):
            replace(candidate("task:124"), dependency_ids=("task:124",))

    def test_head_and_policy_are_bound_into_plan_identity(self) -> None:
        observed = snapshot(candidate("task:124"))
        changed_head = replace(observed, head_sha="c" * 40)

        first = self.planner.plan(observed, policy(), now=NOW)
        second = self.planner.plan(changed_head, policy(), now=NOW)
        third = self.planner.plan(
            observed,
            replace(policy(), max_snapshot_age_seconds=600),
            now=NOW,
        )

        self.assertNotEqual(first.snapshot_digest, second.snapshot_digest)
        self.assertNotEqual(first.digest, second.digest)
        self.assertNotEqual(first.decision.policy_digest, third.decision.policy_digest)
        self.assertNotEqual(first.digest, third.digest)

    def test_planning_time_must_not_precede_observation(self) -> None:
        with self.assertRaisesRegex(ValueError, "precedes snapshot"):
            self.planner.plan(
                snapshot(candidate("task:124")),
                policy(),
                now=CAPTURED_AT - timedelta(microseconds=1),
            )


if __name__ == "__main__":
    unittest.main()
