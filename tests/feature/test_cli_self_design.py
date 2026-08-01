from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import cast

import pytest
from typer.testing import CliRunner

from af.bootstrap import create_application
from af.framework.container import ProviderRegistrar, ServiceProvider
from af.framework.self_design import (
    DesignCandidate,
    DesignPolicy,
    DesignSnapshot,
    DesignUnitKind,
)
from af.framework.self_design_operation import DesignEvidenceProvider, SelfDesignOperation
from af.interfaces.console import create_cli

runner = CliRunner()
_CAPTURED_AT = datetime(2026, 7, 24, 19, 0, tzinfo=UTC)


def _snapshot() -> DesignSnapshot:
    return DesignSnapshot(
        source_id="test.evidence",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha="a" * 40,
        captured_at=_CAPTURED_AT,
        candidates=(
            DesignCandidate(
                candidate_id="issue:124",
                revision="sha256:" + "b" * 64,
                kind=DesignUnitKind.CHILD_TASK,
                priority=0,
                blocking_impact=10,
                risk=1,
                created_at=_CAPTURED_AT,
                objective_id="objective:self-design",
                acceptance_clause_ids=("acceptance:read-only-command",),
                dependency_ids=(),
                active_overlap=False,
                acceptance_complete=True,
                append_only_safe=True,
                authority_granted=True,
                evidence_complete=True,
                complete=False,
            ),
        ),
    )


class RecordingEvidenceProvider:
    def __init__(self, snapshot: DesignSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0

    def capture_snapshot(self) -> DesignSnapshot:
        self.calls += 1
        return self.snapshot


def _operation(provider: DesignEvidenceProvider) -> SelfDesignOperation:
    return SelfDesignOperation(
        provider,
        DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
        lambda: _CAPTURED_AT + timedelta(seconds=1),
    )


class SelfDesignProvider(ServiceProvider):
    provider_id = "test.self-design"

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            SelfDesignOperation,
            lambda _: _operation(RecordingEvidenceProvider(_snapshot())),
        )


def test_self_design_command_emits_one_canonical_read_only_plan() -> None:
    provider = RecordingEvidenceProvider(_snapshot())

    result = runner.invoke(create_cli(self_design_operation=_operation(provider)), ["self-design"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == "propose"
    assert payload["selected_candidate_id"] == "issue:124"
    assert payload["objective_id"] == "objective:self-design"
    assert payload["acceptance_clause_ids"] == ["acceptance:read-only-command"]
    assert payload["allowed_writes"] == []
    assert provider.calls == 1


def test_self_design_command_is_byte_stable_for_identical_evidence() -> None:
    first = RecordingEvidenceProvider(_snapshot())
    second = RecordingEvidenceProvider(_snapshot())

    first_result = runner.invoke(
        create_cli(self_design_operation=_operation(first)),
        ["self-design"],
    )
    second_result = runner.invoke(
        create_cli(self_design_operation=_operation(second)),
        ["self-design"],
    )

    assert first_result.exit_code == 0
    assert second_result.exit_code == 0
    assert first_result.stdout == second_result.stdout


def test_self_design_command_fails_closed_without_an_evidence_provider() -> None:
    result = runner.invoke(create_cli(), ["self-design"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "configuration is invalid\n"
    assert "Traceback" not in result.stderr


def test_self_design_command_rejects_invalid_provider_output() -> None:
    class InvalidEvidenceProvider:
        def capture_snapshot(self) -> DesignSnapshot:
            return cast(DesignSnapshot, object())

    result = runner.invoke(
        create_cli(self_design_operation=_operation(InvalidEvidenceProvider())),
        ["self-design"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "self-design evidence provider returned an invalid snapshot\n"


def test_installed_self_design_command_loads_operation_from_application_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("test_self_design_operator_profile")

    def build_application():  # type: ignore[no-untyped-def]
        return create_application(providers=(SelfDesignProvider(),))

    module.build_application = build_application  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = runner.invoke(
        create_cli(),
        [
            "--application-factory",
            "test_self_design_operator_profile:build_application",
            "self-design",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == "propose"
    assert payload["selected_candidate_id"] == "issue:124"
    assert payload["allowed_writes"] == []


def test_installed_console_retains_existing_commands() -> None:
    result = runner.invoke(create_cli(), ["--help"])

    assert result.exit_code == 0
    assert "self-design" in result.stdout
    assert "hello" in result.stdout
    assert "migrations" in result.stdout
