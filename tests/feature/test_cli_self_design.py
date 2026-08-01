from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from typer.testing import CliRunner

from af.application.self_design_execution import (
    SelfDesignExecutionController,
    SelfDesignExecutionResult,
)
from af.bootstrap import create_application
from af.framework.container import ProviderRegistrar, ServiceProvider
from af.framework.self_design import (
    DesignCandidate,
    DesignPlan,
    DesignPolicy,
    DesignSnapshot,
    DesignUnitKind,
)
from af.framework.self_design_operation import DesignEvidenceProvider, SelfDesignOperation
from af.framework.self_design_publication import (
    DesignPublicationEffect,
    DesignPublicationReceipt,
    DesignPublicationStatus,
)
from af.framework.self_design_recording import (
    DesignPlanRecord,
    DesignRecordAuthority,
    DesignRecordingEffect,
    DesignRecordingRequest,
    DesignRecordResult,
    DesignRecordStatus,
)
from af.framework.version_control import (
    CoordinationObservation,
    GitObjectFormat,
    InitializationStatus,
    RemoteObservation,
    RepositoryObservation,
    VersionControlInitialization,
    VersionControlManifest,
)
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


def _coordination(tmp_path: Path) -> VersionControlInitialization:
    common_dir = tmp_path / ".git"
    af_dir = common_dir / "af"
    af_dir.mkdir(parents=True)
    manifest = VersionControlManifest(
        repository=RepositoryObservation(
            root=str(tmp_path),
            git_common_dir=str(common_dir),
            branch="master",
            head_sha="a" * 40,
            object_format=GitObjectFormat.SHA1,
        ),
        upstream_remote=RemoteObservation(
            name="origin",
            fetch_url="https://example.invalid/AgentFramework.git",
            push_url="https://example.invalid/AgentFramework.git",
        ),
        coordination=CoordinationObservation(
            remote_name="af-local",
            repository_path=str(af_dir / "remotes" / "coordination.git"),
            worktree_root=str(af_dir / "worktrees"),
        ),
    )
    manifest_path = af_dir / "version-control.json"
    manifest_path.write_bytes(manifest.file_bytes())
    return VersionControlInitialization(
        status=InitializationStatus.UNCHANGED,
        manifest=manifest,
        manifest_path=str(manifest_path),
    )


def _execution_result(tmp_path: Path) -> SelfDesignExecutionResult:
    prepared = _operation(RecordingEvidenceProvider(_snapshot())).prepare()
    coordination = _coordination(tmp_path)
    authority = DesignRecordAuthority(
        authority_id="test:record-124",
        repository=prepared.snapshot.repository,
        base_ref=prepared.snapshot.base_ref,
        head_sha=prepared.snapshot.head_sha,
        snapshot_digest=prepared.snapshot.digest,
        plan_digest=prepared.plan.digest,
        version_control_manifest_digest=coordination.manifest.digest(),
        issued_at=_CAPTURED_AT,
        expires_at=_CAPTURED_AT + timedelta(minutes=5),
        allowed_effects=(DesignRecordingEffect.LOCAL_RECORD,),
    )
    record = DesignPlanRecord.from_request(
        DesignRecordingRequest(
            prepared=prepared,
            authority=authority,
            coordination=coordination,
            observed_at=_CAPTURED_AT + timedelta(seconds=2),
        )
    )
    record_path = (
        Path(coordination.manifest.repository.git_common_dir)
        / "af"
        / "self-design"
        / "records"
        / f"{prepared.plan.digest.removeprefix('sha256:')}.json"
    )
    record_path.parent.mkdir(parents=True, exist_ok=True)
    record_path.write_bytes(record.canonical_bytes())
    return SelfDesignExecutionResult(
        plan=prepared.plan,
        record=DesignRecordResult(
            status=DesignRecordStatus.RECORDED,
            record=record,
            record_path=str(record_path),
        ),
        publication=DesignPublicationReceipt(
            status=DesignPublicationStatus.PUBLISHED,
            effect=DesignPublicationEffect.CREATE_ISSUE,
            repository=prepared.snapshot.repository,
            issue_number=146,
            target_revision="sha256:" + "9" * 64,
            draft_digest="sha256:" + "c" * 64,
        ),
    )


class RecordingSelfDesignExecutor:
    def __init__(self, result: SelfDesignExecutionResult) -> None:
        self.result = result
        self.preview_calls = 0
        self.execute_calls = 0

    def preview(self) -> DesignPlan:
        self.preview_calls += 1
        return self.result.plan

    def execute(self) -> SelfDesignExecutionResult:
        self.execute_calls += 1
        return self.result


class SelfDesignExecutionProvider(ServiceProvider):
    provider_id = "test.self-design.execute"

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            SelfDesignExecutionController,
            lambda _: SelfDesignExecutionController(
                RecordingSelfDesignExecutor(_execution_result(self._tmp_path))
            ),
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


def test_self_design_execute_command_previews_without_writes(tmp_path: Path) -> None:
    executor = RecordingSelfDesignExecutor(_execution_result(tmp_path))

    result = runner.invoke(
        create_cli(
            self_design_execution=SelfDesignExecutionController(executor),
        ),
        ["self-design-execute"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["route"] == "propose"
    assert payload["selected_candidate_id"] == "issue:124"
    assert executor.preview_calls == 1
    assert executor.execute_calls == 0


def test_self_design_execute_command_executes_one_configured_governed_effect(
    tmp_path: Path,
) -> None:
    executor = RecordingSelfDesignExecutor(_execution_result(tmp_path))

    result = runner.invoke(
        create_cli(
            self_design_execution=SelfDesignExecutionController(executor),
        ),
        ["self-design-execute", "--execute"],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["plan"]["route"] == "propose"
    assert payload["plan"]["selected_candidate_id"] == "issue:124"
    assert payload["record"]["status"] == "recorded"
    assert payload["publication"]["status"] == "published"
    assert payload["publication"]["issue_number"] == 146
    assert executor.preview_calls == 0
    assert executor.execute_calls == 1


def test_self_design_execute_command_fails_closed_without_controller() -> None:
    result = runner.invoke(create_cli(), ["self-design-execute"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "configuration is invalid\n"


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


def test_installed_self_design_execute_loads_controller_from_application_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = ModuleType("test_self_design_execute_operator_profile")

    def build_application():  # type: ignore[no-untyped-def]
        return create_application(
            providers=(SelfDesignExecutionProvider(tmp_path),),
        )

    module.build_application = build_application  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = runner.invoke(
        create_cli(),
        [
            "--application-factory",
            "test_self_design_execute_operator_profile:build_application",
            "self-design-execute",
            "--execute",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["plan"]["selected_candidate_id"] == "issue:124"
    assert payload["record"]["status"] == "recorded"
    assert payload["publication"]["status"] == "published"


def test_installed_console_retains_existing_commands() -> None:
    result = runner.invoke(create_cli(), ["--help"])

    assert result.exit_code == 0
    assert "self-design" in result.stdout
    assert "self-design-execute" in result.stdout
    assert "hello" in result.stdout
    assert "migrations" in result.stdout
