from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

from typer.testing import CliRunner

from af.framework.self_analysis import (
    AnalysisCandidate,
    AnalysisFocusKind,
    AnalysisPolicy,
    AnalysisSnapshot,
)
from af.framework.self_analysis_operation import AnalysisEvidenceProvider, SelfAnalysisOperation
from af.framework.self_operation import AuthorityRecord, SelfOperationSnapshot, StageBudget
from af.interfaces.console import create_cli

runner = CliRunner()
_CAPTURED_AT = datetime(2026, 7, 24, 20, 0, tzinfo=UTC)


def _snapshot() -> AnalysisSnapshot:
    return AnalysisSnapshot(
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
        candidates=(
            AnalysisCandidate(
                candidate_id="issue:126",
                revision="sha256:" + "b" * 64,
                focus_kind=AnalysisFocusKind.GAP,
                priority=0,
                urgency=10,
                risk=1,
                created_at=_CAPTURED_AT,
                facts=("Issue is ready for a shared stage protocol slice.",),
                hypotheses=("A first read-only stage is still missing.",),
                uncertainties=("No open PR already owns the slice.",),
                causal_claims=("A canonical handoff contract unblocks wrapper stages.",),
                clarification_needs=("Confirm the first read-only stage.",),
                active_overlap=False,
                evidence_complete=True,
                authority_granted=True,
                complete=False,
            ),
        ),
    )


class RecordingEvidenceProvider:
    def __init__(self, snapshot: AnalysisSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def capture_snapshot(self) -> AnalysisSnapshot:
        self.calls += 1
        return self.snapshot


def _operation(provider: AnalysisEvidenceProvider) -> SelfAnalysisOperation:
    return SelfAnalysisOperation(
        provider,
        AnalysisPolicy(
            max_snapshot_age_seconds=60,
            tie_break_by_id=True,
            entry_authority_id="authority:entry-point",
        ),
        lambda: _CAPTURED_AT + timedelta(seconds=1),
    )


def test_self_analysis_command_emits_one_canonical_read_only_plan() -> None:
    provider = RecordingEvidenceProvider(_snapshot())

    result = runner.invoke(
        create_cli(self_analysis_operation=_operation(provider)),
        ["self-analysis"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == "analyse"
    assert payload["selected_candidate_id"] == "issue:126"
    assert payload["focus_kind"] == "gap"
    assert payload["facts"] == ["Issue is ready for a shared stage protocol slice."]
    assert payload["stage_plan"]["stage"] == "analysis"
    assert payload["stage_plan"]["allowed_writes"] == []
    assert provider.calls == 1


def test_self_analysis_command_is_byte_stable_for_identical_evidence() -> None:
    first = RecordingEvidenceProvider(_snapshot())
    second = RecordingEvidenceProvider(_snapshot())

    first_result = runner.invoke(
        create_cli(self_analysis_operation=_operation(first)),
        ["self-analysis"],
    )
    second_result = runner.invoke(
        create_cli(self_analysis_operation=_operation(second)),
        ["self-analysis"],
    )

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert first_result.stdout == second_result.stdout


def test_self_analysis_command_fails_closed_without_an_evidence_provider() -> None:
    result = runner.invoke(create_cli(), ["self-analysis"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "configuration is invalid\n"
    assert "Traceback" not in result.stderr


def test_self_analysis_command_rejects_invalid_provider_output() -> None:
    class InvalidEvidenceProvider:
        def capture_snapshot(self) -> AnalysisSnapshot:
            return cast(AnalysisSnapshot, object())

    result = runner.invoke(
        create_cli(self_analysis_operation=_operation(InvalidEvidenceProvider())),
        ["self-analysis"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "self-analysis evidence provider returned an invalid snapshot\n"


def test_installed_console_retains_existing_commands() -> None:
    result = runner.invoke(create_cli(), ["--help"])

    assert result.exit_code == 0
    assert "self-analysis" in result.stdout
    assert "self-design" in result.stdout
    assert "hello" in result.stdout
    assert "migrations" in result.stdout
