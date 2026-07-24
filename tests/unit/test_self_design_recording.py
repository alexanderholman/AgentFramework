from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.application.self_design_recording import SelfDesignRecordingController
from af.framework.self_design import (
    DesignCandidate,
    DesignPolicy,
    DesignSnapshot,
    DesignUnitKind,
)
from af.framework.self_design_operation import PreparedDesignPlan, SelfDesignOperation
from af.framework.self_design_recording import (
    DesignPlanRecord,
    DesignRecordAuthority,
    DesignRecordingEffect,
    DesignRecordingError,
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

_CAPTURED_AT = datetime(2026, 7, 24, 21, 0, tzinfo=UTC)
_OBSERVED_AT = _CAPTURED_AT + timedelta(seconds=2)


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


class RecordingEvidenceProvider:
    def __init__(self) -> None:
        self.calls = 0

    def capture_snapshot(self) -> DesignSnapshot:
        self.calls += 1
        return _snapshot()


def _operation(provider: RecordingEvidenceProvider | None = None) -> SelfDesignOperation:
    return SelfDesignOperation(
        provider or RecordingEvidenceProvider(),
        DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
        lambda: _CAPTURED_AT + timedelta(seconds=1),
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


def _authority(
    prepared: PreparedDesignPlan,
    coordination: VersionControlInitialization,
) -> DesignRecordAuthority:
    return DesignRecordAuthority(
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


def _request(tmp_path: Path) -> DesignRecordingRequest:
    prepared = _operation().prepare()
    coordination = _coordination(tmp_path)
    return DesignRecordingRequest(
        prepared=prepared,
        authority=_authority(prepared, coordination),
        coordination=coordination,
        observed_at=_OBSERVED_AT,
    )


def test_prepare_captures_once_and_binds_the_exact_snapshot() -> None:
    provider = RecordingEvidenceProvider()

    prepared = _operation(provider).prepare()

    assert provider.calls == 1
    assert prepared.plan.snapshot_digest == prepared.snapshot.digest
    assert prepared.plan.allowed_writes == ()


def test_record_request_and_record_are_canonical_and_exactly_bound(tmp_path: Path) -> None:
    request = _request(tmp_path)

    record = DesignPlanRecord.from_request(request)
    payload = json.loads(record.canonical_bytes())

    assert payload["schema_version"] == "af.self-design-record/v1"
    assert payload["authority"]["allowed_effects"] == ["local_record"]
    assert payload["snapshot_digest"] == request.prepared.snapshot.digest
    assert payload["plan_digest"] == request.prepared.plan.digest
    assert payload["plan"]["allowed_writes"] == []
    assert len(record.digest) == len("sha256:") + 64
    assert record.canonical_bytes() == record.canonical_bytes()


@pytest.mark.parametrize(
    "replacement",
    [
        {"repository": "other/Repository"},
        {"base_ref": "other"},
        {"head_sha": "c" * 40},
        {"snapshot_digest": "sha256:" + "c" * 64},
        {"plan_digest": "sha256:" + "d" * 64},
        {"version_control_manifest_digest": "e" * 64},
    ],
)
def test_record_request_rejects_any_authority_binding_drift(
    tmp_path: Path,
    replacement: dict[str, object],
) -> None:
    request = _request(tmp_path)
    authority = replace(request.authority, **replacement)

    with pytest.raises(DesignRecordingError, match="evidence binding is invalid"):
        DesignRecordingRequest(
            prepared=request.prepared,
            authority=authority,
            coordination=request.coordination,
            observed_at=request.observed_at,
        )


def test_record_request_rejects_expired_authority(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(DesignRecordingError, match="not currently valid"):
        DesignRecordingRequest(
            prepared=request.prepared,
            authority=request.authority,
            coordination=request.coordination,
            observed_at=request.authority.expires_at + timedelta(microseconds=1),
        )


def test_authority_cannot_grant_zero_or_future_effects(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(ValueError, match="grants unsupported effects"):
        replace(request.authority, allowed_effects=())
    with pytest.raises(ValueError, match="lifetime is too long"):
        replace(
            request.authority,
            expires_at=request.authority.issued_at + timedelta(hours=2),
        )


class RecordingRecorder:
    def __init__(self) -> None:
        self.requests: list[DesignRecordingRequest] = []

    def record(self, request: DesignRecordingRequest) -> DesignRecordResult:
        self.requests.append(request)
        record = DesignPlanRecord.from_request(request)
        return DesignRecordResult(
            status=DesignRecordStatus.RECORDED,
            record=record,
            record_path=str(
                Path(request.coordination.manifest.repository.git_common_dir) / "record"
            ),
        )


def test_controller_separates_read_only_prepare_from_explicit_record(tmp_path: Path) -> None:
    provider = RecordingEvidenceProvider()
    recorder = RecordingRecorder()
    controller = SelfDesignRecordingController(_operation(provider), recorder, lambda: _OBSERVED_AT)
    coordination = _coordination(tmp_path)

    prepared = controller.prepare()
    assert recorder.requests == []

    result = controller.record(prepared, _authority(prepared, coordination), coordination)

    assert provider.calls == 1
    assert len(recorder.requests) == 1
    assert recorder.requests[0].prepared is prepared
    assert result.status is DesignRecordStatus.RECORDED
