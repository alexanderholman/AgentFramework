from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

from typer.testing import CliRunner

from af.framework.self_alignment import AlignmentCandidate, AlignmentPolicy, AlignmentSnapshot
from af.framework.self_alignment_operation import AlignmentEvidenceProvider, SelfAlignmentOperation
from af.framework.self_analysis import (
    AnalysisCandidate,
    AnalysisFocusKind,
    AnalysisPolicy,
    AnalysisSnapshot,
    SelfAnalysisPlanner,
)
from af.framework.self_operation import AuthorityRecord, SelfOperationSnapshot, StageBudget
from af.interfaces.console import create_cli

runner = CliRunner()
_CAPTURED_AT = datetime(2026, 7, 24, 22, 0, tzinfo=UTC)


def _analysis_predecessor_plan():
    operation = SelfOperationSnapshot(
        source_id="test.evidence",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha="a" * 40,
        captured_at=_CAPTURED_AT,
        evidence_digests=("sha256:" + "c" * 64,),
        authorities=(
            AuthorityRecord(
                authority_id="authority:entry-point",
                scope_id="scope:read-only-analysis",
                granted=True,
            ),
            AuthorityRecord(
                authority_id="authority:alignment",
                scope_id="scope:read-only-alignment",
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
    snapshot = AnalysisSnapshot(
        operation=operation,
        candidates=(
            AnalysisCandidate(
                candidate_id="issue:126",
                revision="sha256:" + "b" * 64,
                focus_kind=AnalysisFocusKind.GAP,
                priority=0,
                urgency=10,
                risk=1,
                created_at=_CAPTURED_AT,
                facts=("Issue is ready for a shared alignment slice.",),
                hypotheses=("A first successor stage is still missing.",),
                uncertainties=("No open PR already owns the slice.",),
                causal_claims=("A canonical predecessor handoff unblocks alignment.",),
                clarification_needs=("Confirm the first successor stage.",),
                active_overlap=False,
                evidence_complete=True,
                authority_granted=True,
                complete=False,
            ),
        ),
    )
    return SelfAnalysisPlanner().plan(
        snapshot,
        AnalysisPolicy(
            max_snapshot_age_seconds=60,
            tie_break_by_id=True,
            entry_authority_id="authority:entry-point",
            entry_scope_id="scope:read-only-analysis",
        ),
        now=_CAPTURED_AT + timedelta(seconds=1),
    )


def _snapshot() -> AlignmentSnapshot:
    predecessor = _analysis_predecessor_plan()
    return AlignmentSnapshot(
        operation=SelfOperationSnapshot(
            source_id="test.evidence",
            repository="alexanderholman/AgentFramework",
            base_ref="master",
            head_sha="a" * 40,
            captured_at=_CAPTURED_AT,
            evidence_digests=("sha256:" + "c" * 64,),
            authorities=(
                AuthorityRecord(
                    authority_id="authority:entry-point",
                    scope_id="scope:read-only-analysis",
                    granted=True,
                ),
                AuthorityRecord(
                    authority_id="authority:alignment",
                    scope_id="scope:read-only-alignment",
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
        ),
        predecessor_plan=predecessor,
        candidates=(
            AlignmentCandidate(
                candidate_id="alignment:126",
                revision="sha256:" + "b" * 64,
                objective_id="objective:self-align",
                priority=0,
                risk=1,
                created_at=_CAPTURED_AT,
                operator_intent_digest="sha256:" + "d" * 64,
                architecture_digest="sha256:" + "e" * 64,
                commitment_ids=("commitment:issue-126",),
                constraint_ids=("constraint:read-only",),
                unresolved_conflict_ids=(),
                active_overlap=False,
                intent_preserved=True,
                policy_consistent=True,
                architecture_consistent=True,
                commitments_consistent=True,
                authority_granted=True,
                evidence_complete=True,
                complete=False,
            ),
        ),
    )


class RecordingEvidenceProvider:
    def __init__(self, snapshot: AlignmentSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def capture_snapshot(self) -> AlignmentSnapshot:
        self.calls += 1
        return self.snapshot


def _operation(provider: AlignmentEvidenceProvider) -> SelfAlignmentOperation:
    return SelfAlignmentOperation(
        provider,
        AlignmentPolicy(
            max_snapshot_age_seconds=60,
            tie_break_by_id=True,
            required_authority_id="authority:alignment",
            required_scope_id="scope:read-only-alignment",
        ),
        lambda: _CAPTURED_AT + timedelta(seconds=1),
    )


def test_self_align_command_emits_one_canonical_read_only_plan() -> None:
    provider = RecordingEvidenceProvider(_snapshot())

    result = runner.invoke(
        create_cli(self_alignment_operation=_operation(provider)), ["self-align"]
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == "align"
    assert payload["selected_candidate_id"] == "alignment:126"
    assert payload["objective_id"] == "objective:self-align"
    assert payload["operator_intent_digest"] == "sha256:" + "d" * 64
    assert payload["architecture_digest"] == "sha256:" + "e" * 64
    assert payload["constraint_ids"] == ["constraint:read-only"]
    assert payload["stage_plan"]["stage"] == "alignment"
    assert payload["stage_plan"]["allowed_writes"] == []
    assert payload["stage_plan"]["handoff"]["predecessor_result_digest"] is not None
    assert provider.calls == 1


def test_self_align_command_is_byte_stable_for_identical_evidence() -> None:
    first = RecordingEvidenceProvider(_snapshot())
    second = RecordingEvidenceProvider(_snapshot())

    first_result = runner.invoke(
        create_cli(self_alignment_operation=_operation(first)), ["self-align"]
    )
    second_result = runner.invoke(
        create_cli(self_alignment_operation=_operation(second)), ["self-align"]
    )

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert first_result.stdout == second_result.stdout


def test_self_align_command_fails_closed_without_an_evidence_provider() -> None:
    result = runner.invoke(create_cli(), ["self-align"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "configuration is invalid\n"
    assert "Traceback" not in result.stderr


def test_self_align_command_rejects_invalid_provider_output() -> None:
    class InvalidEvidenceProvider:
        def capture_snapshot(self) -> AlignmentSnapshot:
            return cast(AlignmentSnapshot, object())

    result = runner.invoke(
        create_cli(self_alignment_operation=_operation(InvalidEvidenceProvider())),
        ["self-align"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "self-alignment evidence provider returned an invalid snapshot\n"


def test_installed_console_retains_existing_commands() -> None:
    result = runner.invoke(create_cli(), ["--help"])

    assert result.exit_code == 0
    assert "self-align" in result.stdout
    assert "self-analysis" in result.stdout
    assert "self-design" in result.stdout
    assert "hello" in result.stdout
    assert "migrations" in result.stdout
