from __future__ import annotations

import json
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from typing import cast

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

CAPTURED_AT = datetime(2026, 7, 24, 12, 30, tzinfo=UTC)
HEAD_SHA = "a" * 40
SNAPSHOT_DIGEST = "sha256:" + "b" * 64
POLICY_DIGEST = "sha256:" + "c" * 64
OUTPUT_DIGEST = "sha256:" + "d" * 64


def snapshot() -> SelfOperationSnapshot:
    return SelfOperationSnapshot(
        source_id="github",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha=HEAD_SHA,
        captured_at=CAPTURED_AT,
        evidence_digests=("sha256:" + "f" * 64, "sha256:" + "e" * 64),
        authorities=(
            AuthorityRecord(
                authority_id="authority:entry-point",
                scope_id="scope:read-only-analysis",
                granted=True,
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


class SelfOperationProtocolTests(unittest.TestCase):
    def test_public_payload_apis_match_canonical_json(self) -> None:
        observed = snapshot()
        handoff = StageHandoff(
            stage=SelfOperationStage.ANALYSIS,
            source_snapshot_digest=observed.digest,
            input_artifact_digest=SNAPSHOT_DIGEST,
            policy_digest=POLICY_DIGEST,
            predecessor_result_digest=None,
            entry_authority_id="authority:entry-point",
        )
        result = StageResult(
            stage=SelfOperationStage.ANALYSIS,
            handoff_digest=handoff.digest,
            reason=StageReason.SELECTED,
            output_digest=OUTPUT_DIGEST,
        )
        plan = StagePlan(
            stage=SelfOperationStage.ANALYSIS,
            handoff=handoff,
            result=result,
            route="analyse",
            allowed_writes=(),
            stop_conditions=("authority_missing", "snapshot_stale", "unsafe"),
        )

        self.assertEqual(json.loads(observed.canonical_json()), observed.payload())
        self.assertEqual(json.loads(handoff.canonical_json()), handoff.payload())
        self.assertEqual(json.loads(result.canonical_json()), result.payload())
        self.assertEqual(json.loads(plan.canonical_json()), plan.payload())

    def test_snapshot_digest_is_stable_when_inputs_are_reordered(self) -> None:
        first = snapshot()
        reordered = replace(
            first,
            evidence_digests=tuple(reversed(first.evidence_digests)),
            authorities=tuple(reversed(first.authorities)),
            budgets=tuple(reversed(first.budgets)),
        )

        self.assertEqual(first.canonical_json(), reordered.canonical_json())
        self.assertEqual(first.digest, reordered.digest)

    def test_stage_handoff_requires_exactly_one_entry_path(self) -> None:
        observed = snapshot()

        with self.assertRaisesRegex(ValueError, "entry path"):
            StageHandoff(
                stage=SelfOperationStage.ANALYSIS,
                source_snapshot_digest=observed.digest,
                input_artifact_digest=SNAPSHOT_DIGEST,
                policy_digest=POLICY_DIGEST,
                predecessor_result_digest=None,
                entry_authority_id=None,
            )

        with self.assertRaisesRegex(ValueError, "entry path"):
            StageHandoff(
                stage=SelfOperationStage.ANALYSIS,
                source_snapshot_digest=observed.digest,
                input_artifact_digest=SNAPSHOT_DIGEST,
                policy_digest=POLICY_DIGEST,
                predecessor_result_digest=OUTPUT_DIGEST,
                entry_authority_id="authority:entry-point",
            )

    def test_stage_plan_binds_handoff_result_and_rejects_writes(self) -> None:
        observed = snapshot()
        handoff = StageHandoff(
            stage=SelfOperationStage.ANALYSIS,
            source_snapshot_digest=observed.digest,
            input_artifact_digest=SNAPSHOT_DIGEST,
            policy_digest=POLICY_DIGEST,
            predecessor_result_digest=None,
            entry_authority_id="authority:entry-point",
        )
        result = StageResult(
            stage=SelfOperationStage.ANALYSIS,
            handoff_digest=handoff.digest,
            reason=StageReason.SELECTED,
            output_digest=OUTPUT_DIGEST,
        )

        plan = StagePlan(
            stage=SelfOperationStage.ANALYSIS,
            handoff=handoff,
            result=result,
            route="analyse",
            allowed_writes=(),
            stop_conditions=("authority_missing", "snapshot_stale", "unsafe"),
        )

        self.assertEqual(plan.stage, SelfOperationStage.ANALYSIS)
        self.assertEqual(plan.result.handoff_digest, handoff.digest)
        self.assertEqual(plan.allowed_writes, ())

        with self.assertRaisesRegex(ValueError, "cannot grant writes"):
            replace(plan, allowed_writes=("git:push",))

        stopped = StagePlan(
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
        )
        self.assertEqual(stopped.route, "stop")

    def test_snapshot_and_protocol_validation_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "authority scope"):
            AuthorityRecord(
                authority_id="authority:entry-point",
                scope_id="",
                granted=True,
            )
        with self.assertRaisesRegex(ValueError, "budget usage exceeds limit"):
            StageBudget(
                budget_id="budget:read-only-cycle",
                unit="cycles",
                limit=1,
                used=2,
            )
        with self.assertRaisesRegex(ValueError, "evidence digests"):
            replace(snapshot(), evidence_digests=(SNAPSHOT_DIGEST, SNAPSHOT_DIGEST))
        with self.assertRaisesRegex(ValueError, "authority identities"):
            replace(
                snapshot(),
                authorities=(
                    AuthorityRecord(
                        authority_id="authority:entry-point",
                        scope_id="scope:other",
                        granted=True,
                    ),
                    AuthorityRecord(
                        authority_id="authority:entry-point",
                        scope_id="scope:read-only-analysis",
                        granted=True,
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "budget identities"):
            replace(
                snapshot(),
                budgets=(
                    StageBudget(
                        budget_id="budget:read-only-cycle",
                        unit="cycles",
                        limit=1,
                        used=0,
                    ),
                    StageBudget(
                        budget_id="budget:read-only-cycle",
                        unit="cycles",
                        limit=2,
                        used=1,
                    ),
                ),
            )
        with self.assertRaisesRegex(ValueError, "handoff stage"):
            StageHandoff(
                stage=cast(SelfOperationStage, object()),
                source_snapshot_digest=snapshot().digest,
                input_artifact_digest=SNAPSHOT_DIGEST,
                policy_digest=POLICY_DIGEST,
                predecessor_result_digest=None,
                entry_authority_id="authority:entry-point",
            )
        with self.assertRaisesRegex(ValueError, "entry authority identity"):
            StageHandoff(
                stage=SelfOperationStage.ANALYSIS,
                source_snapshot_digest=snapshot().digest,
                input_artifact_digest=SNAPSHOT_DIGEST,
                policy_digest=POLICY_DIGEST,
                predecessor_result_digest=None,
                entry_authority_id="",
            )
        handoff = StageHandoff(
            stage=SelfOperationStage.ANALYSIS,
            source_snapshot_digest=snapshot().digest,
            input_artifact_digest=SNAPSHOT_DIGEST,
            policy_digest=POLICY_DIGEST,
            predecessor_result_digest=None,
            entry_authority_id="authority:entry-point",
        )
        with self.assertRaisesRegex(ValueError, "output is inconsistent"):
            StageResult(
                stage=SelfOperationStage.ANALYSIS,
                handoff_digest=handoff.digest,
                reason=StageReason.NO_ELIGIBLE_INPUT,
                output_digest=OUTPUT_DIGEST,
            )
        with self.assertRaisesRegex(ValueError, "route is inconsistent"):
            StagePlan(
                stage=SelfOperationStage.ANALYSIS,
                handoff=handoff,
                result=StageResult(
                    stage=SelfOperationStage.ANALYSIS,
                    handoff_digest=handoff.digest,
                    reason=StageReason.SELECTED,
                    output_digest=OUTPUT_DIGEST,
                ),
                route="stop",
                allowed_writes=(),
                stop_conditions=("authority_missing", "snapshot_stale", "unsafe"),
            )
        with self.assertRaisesRegex(ValueError, "stop conditions"):
            StagePlan(
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
                stop_conditions=(),
            )


if __name__ == "__main__":
    unittest.main()
