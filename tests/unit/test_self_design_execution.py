from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.application.self_design_execution import (
    GovernedSelfDesignExecutor,
    SelfDesignExecutionController,
    SelfDesignExecutionError,
    SelfDesignExecutionResult,
)
from af.application.self_design_publication import SelfDesignPublicationController
from af.application.self_design_recording import SelfDesignRecordingController
from af.framework.self_design import (
    DesignCandidate,
    DesignPolicy,
    DesignRoute,
    DesignSnapshot,
    DesignUnitKind,
)
from af.framework.self_design_operation import PreparedDesignPlan, SelfDesignOperation
from af.framework.self_design_publication import (
    DesignPublicationDraft,
    DesignPublicationEffect,
    DesignPublicationReceipt,
    DesignPublicationRequest,
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

_CAPTURED_AT = datetime(2026, 8, 1, 20, 0, tzinfo=UTC)


class ExecutionEvidenceProvider:
    def capture_snapshot(self) -> DesignSnapshot:
        return DesignSnapshot(
            source_id="test.execution",
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
                    acceptance_clause_ids=("acceptance:execute",),
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


def _prepared() -> PreparedDesignPlan:
    return SelfDesignOperation(
        ExecutionEvidenceProvider(),
        DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
        lambda: _CAPTURED_AT + timedelta(seconds=1),
    ).prepare()


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


def _result(tmp_path: Path) -> SelfDesignExecutionResult:
    prepared = _prepared()
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


class RecordingExecutor:
    def __init__(self, result: SelfDesignExecutionResult) -> None:
        self.result = result
        self.calls = 0

    def preview(self) -> DesignPlan:
        return self.result.plan

    def execute(self) -> SelfDesignExecutionResult:
        self.calls += 1
        return self.result


class CoordinationLoader:
    def __init__(self, coordination: VersionControlInitialization) -> None:
        self.coordination = coordination
        self.calls = 0

    def load(self) -> VersionControlInitialization:
        self.calls += 1
        return self.coordination


class DraftBuilder:
    def __init__(self) -> None:
        self.calls = 0

    def build(self, prepared: PreparedDesignPlan) -> DesignPublicationDraft:
        self.calls += 1
        assert prepared.plan.selected_candidate_id is not None
        assert prepared.plan.unit_kind is DesignUnitKind.CHILD_TASK
        assert prepared.plan.objective_id is not None
        return DesignPublicationDraft(
            effect=DesignPublicationEffect.CREATE_ISSUE,
            repository=prepared.snapshot.repository,
            selected_candidate_id=prepared.plan.selected_candidate_id,
            unit_kind=prepared.plan.unit_kind,
            objective_id=prepared.plan.objective_id,
            acceptance_clause_ids=prepared.plan.acceptance_clause_ids,
            target_issue_number=None,
            expected_target_revision=None,
            title="Implement the admitted self-design child task",
            body="Objective and acceptance trace are carried by typed fields.",
        )


class Recorder:
    def __init__(self) -> None:
        self.requests: list[DesignRecordingRequest] = []

    def record(self, request: DesignRecordingRequest) -> DesignRecordResult:
        self.requests.append(request)
        record = DesignPlanRecord.from_request(request)
        path = (
            Path(request.coordination.manifest.repository.git_common_dir)
            / "af"
            / "self-design"
            / "records"
            / f"{request.prepared.plan.digest.removeprefix('sha256:')}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(record.canonical_bytes())
        return DesignRecordResult(
            status=DesignRecordStatus.RECORDED,
            record=record,
            record_path=str(path),
        )


class Publisher:
    def __init__(self) -> None:
        self.requests: list[DesignPublicationRequest] = []

    def publish(self, request: DesignPublicationRequest) -> DesignPublicationReceipt:
        self.requests.append(request)
        return DesignPublicationReceipt(
            status=DesignPublicationStatus.PUBLISHED,
            effect=request.draft.effect,
            repository=request.draft.repository,
            issue_number=146,
            target_revision="sha256:" + "9" * 64,
            draft_digest=request.draft.digest,
        )


def test_execution_controller_runs_executor_once_and_returns_typed_result(tmp_path: Path) -> None:
    executor = RecordingExecutor(_result(tmp_path))
    controller = SelfDesignExecutionController(executor)

    result = controller.execute()

    assert executor.calls == 1
    assert result.plan.selected_candidate_id == "issue:124"
    assert result.record.status is DesignRecordStatus.RECORDED
    assert result.publication.status is DesignPublicationStatus.PUBLISHED


def test_execution_controller_rejects_invalid_executor_result() -> None:
    class InvalidExecutor:
        def preview(self) -> object:
            return object()

        def execute(self) -> object:
            return object()

    controller = SelfDesignExecutionController(InvalidExecutor())

    with pytest.raises(TypeError, match="invalid result"):
        controller.execute()


def test_execution_result_serializes_plan_record_and_publication(tmp_path: Path) -> None:
    payload = _result(tmp_path).to_dict()

    assert payload["plan"]["selected_candidate_id"] == "issue:124"
    assert payload["record"]["status"] == "recorded"
    assert payload["publication"]["status"] == "published"
    assert payload["publication"]["issue_number"] == 146


def test_governed_executor_issues_exact_record_and_publication_authorities(
    tmp_path: Path,
) -> None:
    coordination = _coordination(tmp_path)
    loader = CoordinationLoader(coordination)
    draft_builder = DraftBuilder()
    recorder = Recorder()
    publisher = Publisher()
    executor = GovernedSelfDesignExecutor(
        SelfDesignRecordingController(
            SelfDesignOperation(
                ExecutionEvidenceProvider(),
                DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
                lambda: _CAPTURED_AT + timedelta(seconds=1),
            ),
            recorder,
            lambda: _CAPTURED_AT + timedelta(seconds=2),
        ),
        loader,
        draft_builder,
        SelfDesignPublicationController(publisher, lambda: _CAPTURED_AT + timedelta(seconds=3)),
        lambda: _CAPTURED_AT,
    )

    result = executor.execute()

    assert result.plan.route is DesignRoute.PROPOSE
    assert loader.calls == 1
    assert draft_builder.calls == 1
    assert len(recorder.requests) == 1
    assert len(publisher.requests) == 1
    record_request = recorder.requests[0]
    publication_request = publisher.requests[0]
    assert record_request.authority.snapshot_digest == result.plan.snapshot_digest
    assert publication_request.authority.record_digest == result.record.record.digest
    assert publication_request.authority.draft_digest == publication_request.draft.digest


def test_governed_executor_rejects_stop_plans_before_writes(tmp_path: Path) -> None:
    class EmptyEvidenceProvider:
        def capture_snapshot(self) -> DesignSnapshot:
            return DesignSnapshot(
                source_id="test.execution",
                repository="alexanderholman/AgentFramework",
                base_ref="master",
                head_sha="a" * 40,
                captured_at=_CAPTURED_AT,
                candidates=(),
            )

    loader = CoordinationLoader(_coordination(tmp_path))
    draft_builder = DraftBuilder()
    recorder = Recorder()
    publisher = Publisher()
    executor = GovernedSelfDesignExecutor(
        SelfDesignRecordingController(
            SelfDesignOperation(
                EmptyEvidenceProvider(),
                DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
                lambda: _CAPTURED_AT + timedelta(seconds=1),
            ),
            recorder,
            lambda: _CAPTURED_AT + timedelta(seconds=2),
        ),
        loader,
        draft_builder,
        SelfDesignPublicationController(publisher, lambda: _CAPTURED_AT + timedelta(seconds=3)),
        lambda: _CAPTURED_AT,
    )

    with pytest.raises(SelfDesignExecutionError, match="requires a proposed design unit"):
        executor.execute()

    assert draft_builder.calls == 0
    assert recorder.requests == []
    assert publisher.requests == []
