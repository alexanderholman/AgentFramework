from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.framework.self_design import (
    DesignCandidate,
    DesignPolicy,
    DesignSnapshot,
    DesignUnitKind,
)
from af.framework.self_design_operation import SelfDesignOperation
from af.framework.self_design_recording import (
    DesignRecordAuthority,
    DesignRecordStatus,
    DesignRecordingEffect,
    DesignRecordingError,
    DesignRecordingRequest,
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
from af.infrastructure.local_self_design_recording import LocalDesignPlanRecorder

_CAPTURED_AT = datetime(2026, 7, 24, 21, 0, tzinfo=UTC)
_OBSERVED_AT = _CAPTURED_AT + timedelta(seconds=2)


def _prepared():  # type: ignore[no-untyped-def]
    snapshot = DesignSnapshot(
        source_id="test.integration",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha="a" * 40,
        captured_at=_CAPTURED_AT,
        candidates=(
            DesignCandidate(
                candidate_id="issue:124",
                revision="sha256:" + "b" * 64,
                kind=DesignUnitKind.ACCEPTANCE_REPAIR,
                priority=0,
                blocking_impact=10,
                risk=1,
                created_at=_CAPTURED_AT,
                objective_id="objective:self-design",
                acceptance_clause_ids=("acceptance:local-record",),
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

    class Provider:
        def capture_snapshot(self) -> DesignSnapshot:
            return snapshot

    return SelfDesignOperation(
        Provider(),
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


def _request(tmp_path: Path) -> DesignRecordingRequest:
    prepared = _prepared()
    coordination = _coordination(tmp_path)
    authority = DesignRecordAuthority(
        authority_id="integration:record-124",
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
    return DesignRecordingRequest(
        prepared=prepared,
        authority=authority,
        coordination=coordination,
        observed_at=_OBSERVED_AT,
    )


def test_local_recorder_writes_once_and_then_reports_unchanged(tmp_path: Path) -> None:
    request = _request(tmp_path)
    recorder = LocalDesignPlanRecorder()

    first = recorder.record(request)
    second = recorder.record(request)

    assert first.status is DesignRecordStatus.RECORDED
    assert second.status is DesignRecordStatus.UNCHANGED
    assert first.record_path == second.record_path
    payload = json.loads(Path(first.record_path).read_bytes())
    assert payload["plan_digest"] == request.prepared.plan.digest
    assert payload["snapshot_digest"] == request.prepared.snapshot.digest
    assert payload["authority"]["allowed_effects"] == ["local_record"]
    assert first.to_dict()["record"]["path"] == first.record_path


def test_local_recorder_rejects_manifest_tampering_before_write(tmp_path: Path) -> None:
    request = _request(tmp_path)
    Path(request.coordination.manifest_path).write_text("{}\n", encoding="utf-8")

    with pytest.raises(DesignRecordingError, match="coordination manifest is invalid"):
        LocalDesignPlanRecorder().record(request)

    assert not (tmp_path / ".git" / "af" / "self-design").exists()


def test_local_recorder_rejects_conflicting_existing_record(tmp_path: Path) -> None:
    request = _request(tmp_path)
    recorder = LocalDesignPlanRecorder()
    result = recorder.record(request)
    Path(result.record_path).write_text("{}\n", encoding="utf-8")

    with pytest.raises(DesignRecordingError, match="conflicts with existing state"):
        recorder.record(request)


def test_local_recorder_rejects_redirected_managed_directory(tmp_path: Path) -> None:
    request = _request(tmp_path)
    target = tmp_path / "outside"
    target.mkdir()
    managed = tmp_path / ".git" / "af" / "self-design"
    try:
        managed.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(DesignRecordingError, match="managed state is invalid"):
        LocalDesignPlanRecorder().record(request)
