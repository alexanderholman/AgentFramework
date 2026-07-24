from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from af.framework.self_development import (
    CycleRoute,
    DevelopmentSnapshot,
    SelectionPolicy,
    SelectionReason,
    SelfDevelopmentPlanner,
    WorkCandidate,
    WorkKind,
)

CAPTURED_AT = datetime(2026, 7, 24, 11, 30, tzinfo=UTC)
NOW = CAPTURED_AT + timedelta(minutes=1)
HEAD_SHA = "a" * 40
EVIDENCE_DIGEST = "sha256:" + "b" * 64


def candidate(
    item_id: str,
    *,
    priority: int = 1,
    blocking_impact: int = 1,
    risk: int = 1,
    age_minutes: int = 1,
    procedure_id: str | None = None,
    dependencies_ready: bool = True,
    active_overlap: bool = False,
    scope_clear: bool = True,
    authority_granted: bool = True,
    policy_allowed: bool = True,
    evidence_passed: bool = True,
    complete: bool = False,
) -> WorkCandidate:
    return WorkCandidate(
        item_id=item_id,
        revision=EVIDENCE_DIGEST,
        kind=WorkKind.IMPLEMENTATION,
        priority=priority,
        blocking_impact=blocking_impact,
        risk=risk,
        created_at=CAPTURED_AT - timedelta(minutes=age_minutes),
        procedure_id=procedure_id,
        dependencies_ready=dependencies_ready,
        active_overlap=active_overlap,
        scope_clear=scope_clear,
        authority_granted=authority_granted,
        policy_allowed=policy_allowed,
        evidence_passed=evidence_passed,
        complete=complete,
    )


def snapshot(*candidates: WorkCandidate) -> DevelopmentSnapshot:
    return DevelopmentSnapshot(
        source_id="github",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha=HEAD_SHA,
        captured_at=CAPTURED_AT,
        candidates=candidates,
    )


def policy(*, tie_break_by_id: bool = False) -> SelectionPolicy:
    return SelectionPolicy(
        max_snapshot_age_seconds=300,
        tie_break_by_id=tie_break_by_id,
        required_gates=("governance", "pytest", "ruff", "mypy"),
    )


class SelfDevelopmentPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = SelfDevelopmentPlanner()

    def test_same_snapshot_and_policy_produce_byte_identical_plan(self) -> None:
        observed = snapshot(candidate("issue:120", procedure_id="implement-self-development"))

        first = self.planner.plan(observed, policy(), now=NOW)
        second = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(first.canonical_json(), second.canonical_json())
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(first.route, CycleRoute.REUSE)
        self.assertEqual(first.selected_item_id, "issue:120")
        self.assertEqual(first.procedure_id, "implement-self-development")
        self.assertEqual(first.allowed_writes, ())

    def test_source_order_does_not_change_snapshot_or_plan(self) -> None:
        lower = candidate("issue:120", priority=0)
        higher = candidate("issue:121", priority=1)

        forward = snapshot(lower, higher)
        reverse = snapshot(higher, lower)

        self.assertEqual(forward.digest, reverse.digest)
        self.assertEqual(
            self.planner.plan(forward, policy(), now=NOW).canonical_json(),
            self.planner.plan(reverse, policy(), now=NOW).canonical_json(),
        )

    def test_selection_orders_by_priority_impact_risk_then_age(self) -> None:
        observed = snapshot(
            candidate("issue:priority", priority=0, blocking_impact=0, risk=9),
            candidate("issue:impact-low", priority=1, blocking_impact=1),
            candidate("issue:impact-high", priority=1, blocking_impact=9, risk=9),
            candidate("issue:risk-low", priority=1, blocking_impact=9, risk=1),
            candidate(
                "issue:oldest",
                priority=1,
                blocking_impact=9,
                risk=1,
                age_minutes=10,
            ),
        )

        selected = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(selected.selected_item_id, "issue:priority")

        without_priority = snapshot(
            candidate("issue:impact-low", priority=1, blocking_impact=1),
            candidate("issue:impact-high", priority=1, blocking_impact=9, risk=9),
            candidate("issue:risk-low", priority=1, blocking_impact=9, risk=1),
            candidate(
                "issue:oldest",
                priority=1,
                blocking_impact=9,
                risk=1,
                age_minutes=10,
            ),
        )
        selected = self.planner.plan(without_priority, policy(), now=NOW)
        self.assertEqual(selected.selected_item_id, "issue:oldest")

    def test_exact_tie_fails_closed_without_explicit_tie_break(self) -> None:
        observed = snapshot(
            candidate("issue:120"),
            candidate("issue:121"),
        )

        stopped = self.planner.plan(observed, policy(), now=NOW)

        self.assertEqual(stopped.route, CycleRoute.STOP)
        self.assertEqual(stopped.selection.reason, SelectionReason.AMBIGUOUS_SELECTION)
        self.assertIsNone(stopped.selected_item_id)

        selected = self.planner.plan(
            observed,
            policy(tie_break_by_id=True),
            now=NOW,
        )
        self.assertEqual(selected.selected_item_id, "issue:120")

    def test_stale_snapshot_stops_before_candidate_selection(self) -> None:
        stopped = self.planner.plan(
            snapshot(candidate("issue:120")),
            policy(),
            now=CAPTURED_AT + timedelta(seconds=301),
        )

        self.assertEqual(stopped.route, CycleRoute.STOP)
        self.assertEqual(stopped.selection.reason, SelectionReason.STALE_SNAPSHOT)
        self.assertEqual(
            {item.reason for item in stopped.selection.assessments},
            {SelectionReason.STALE_SNAPSHOT},
        )

    def test_each_ineligible_fact_has_a_stable_reason_code(self) -> None:
        observed = snapshot(
            candidate("issue:dependency", dependencies_ready=False),
            candidate("issue:overlap", active_overlap=True),
            candidate("issue:scope", scope_clear=False),
            candidate("issue:authority", authority_granted=False),
            candidate("issue:policy", policy_allowed=False),
            candidate("issue:evidence", evidence_passed=False),
            candidate("issue:complete", complete=True),
        )

        stopped = self.planner.plan(observed, policy(), now=NOW)
        reasons = {
            assessment.item_id: assessment.reason for assessment in stopped.selection.assessments
        }

        self.assertEqual(stopped.selection.reason, SelectionReason.NO_ELIGIBLE_WORK)
        self.assertEqual(
            reasons,
            {
                "issue:authority": SelectionReason.MISSING_AUTHORITY,
                "issue:complete": SelectionReason.COMPLETE,
                "issue:dependency": SelectionReason.BLOCKED_DEPENDENCY,
                "issue:evidence": SelectionReason.FAILED_EVIDENCE,
                "issue:overlap": SelectionReason.ACTIVE_OVERLAP,
                "issue:policy": SelectionReason.POLICY_DENIED,
                "issue:scope": SelectionReason.AMBIGUOUS_SCOPE,
            },
        )

    def test_eligible_gap_routes_to_governed_engineering_without_write_authority(self) -> None:
        plan = self.planner.plan(
            snapshot(candidate("issue:120")),
            policy(),
            now=NOW,
        )

        self.assertEqual(plan.route, CycleRoute.ENGINEER_GAP)
        self.assertEqual(plan.selected_item_id, "issue:120")
        self.assertIsNone(plan.procedure_id)
        self.assertEqual(plan.allowed_writes, ())
        self.assertEqual(
            plan.stop_conditions,
            (
                "ambiguity",
                "authority_missing",
                "budget_exhausted",
                "cancelled",
                "evidence_failed",
                "snapshot_stale",
                "unsafe",
            ),
        )

    def test_snapshot_rejects_duplicate_or_malformed_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "candidate identities must be unique"):
            snapshot(candidate("issue:120"), candidate("issue:120"))

        with self.assertRaisesRegex(ValueError, "candidates are invalid"):
            snapshot(*(candidate(f"issue:{index}") for index in range(1_001)))

        with self.assertRaisesRegex(ValueError, "head SHA"):
            replace(snapshot(candidate("issue:120")), head_sha="master")

        with self.assertRaisesRegex(ValueError, "revision"):
            replace(candidate("issue:120"), revision="unverified")

        with self.assertRaisesRegex(ValueError, "aware"):
            replace(candidate("issue:120"), created_at=datetime(2026, 7, 24))

    def test_candidate_and_policy_bounds_fail_closed(self) -> None:
        for field_name, value in (
            ("priority", 10),
            ("blocking_impact", 10_001),
            ("risk", 10_001),
        ):
            with (
                self.subTest(field_name=field_name),
                self.assertRaisesRegex(ValueError, field_name.replace("_", " ")),
            ):
                replace(candidate("issue:120"), **{field_name: value})

        with self.assertRaisesRegex(ValueError, "procedure identity"):
            replace(candidate("issue:120"), procedure_id="Not Canonical")

        with self.assertRaisesRegex(ValueError, "eligibility facts"):
            replace(candidate("issue:120"), dependencies_ready=1)

        for invalid_fields in (
            {"max_snapshot_age_seconds": 0},
            {"max_snapshot_age_seconds": 86_401},
            {"required_gates": ()},
            {"required_gates": ("pytest", "pytest")},
        ):
            with self.subTest(invalid_fields=invalid_fields), self.assertRaises(ValueError):
                replace(policy(), **invalid_fields)

    def test_snapshot_head_is_bound_into_digest(self) -> None:
        first = snapshot(candidate("issue:120"))
        second = replace(first, head_sha="c" * 40)

        self.assertNotEqual(first.digest, second.digest)

    def test_policy_is_bound_into_plan_identity(self) -> None:
        observed = snapshot(candidate("issue:120"))
        first = self.planner.plan(observed, policy(), now=NOW)
        second = self.planner.plan(
            observed,
            replace(policy(), max_snapshot_age_seconds=600),
            now=NOW,
        )

        self.assertNotEqual(
            first.selection.policy_digest,
            second.selection.policy_digest,
        )
        self.assertNotEqual(first.digest, second.digest)

    def test_planning_time_must_not_precede_observation(self) -> None:
        with self.assertRaisesRegex(ValueError, "precedes snapshot"):
            self.planner.plan(
                snapshot(candidate("issue:120")),
                policy(),
                now=CAPTURED_AT - timedelta(microseconds=1),
            )

    def test_empty_snapshot_returns_typed_stop_plan(self) -> None:
        stopped = self.planner.plan(snapshot(), policy(), now=NOW)

        self.assertEqual(stopped.route, CycleRoute.STOP)
        self.assertEqual(stopped.selection.reason, SelectionReason.NO_ELIGIBLE_WORK)
        self.assertEqual(stopped.selection.assessments, ())


if __name__ == "__main__":
    unittest.main()
