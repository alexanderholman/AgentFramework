from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.application.self_design_publication import SelfDesignPublicationController
from af.framework.self_design import (
    DesignCandidate,
    DesignPolicy,
    DesignSnapshot,
    DesignUnitKind,
)
from af.framework.self_design_operation import PreparedDesignPlan, SelfDesignOperation
from af.framework.self_design_publication import (
    DesignPublicationAuthority,
    DesignPublicationDraft,
    DesignPublicationEffect,
    DesignPublicationError,
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

_CAPTURED_AT = datetime(2026, 7, 25, 1, 30, tzinfo=UTC)
_OBSERVED_AT = _CAPTURED_AT + timedelta(seconds=3)


class PublicationEvidenceProvider:
    def __init__(self, kind: DesignUnitKind = DesignUnitKind.CHILD_TASK) -> None:
        self.kind = kind

    def capture_snapshot(self) -> DesignSnapshot:
        return _snapshot(self.kind)


class EmptyPublicationEvidenceProvider:
    def capture_snapshot(self) -> DesignSnapshot:
        return _snapshot(candidates=())


def _candidate(kind: DesignUnitKind = DesignUnitKind.CHILD_TASK) -> DesignCandidate:
    return DesignCandidate(
        candidate_id="issue:124",
        revision="sha256:" + "b" * 64,
        kind=kind,
        priority=0,
        blocking_impact=10,
        risk=1,
        created_at=_CAPTURED_AT,
        objective_id="objective:self-design",
        acceptance_clause_ids=("acceptance:publish", "acceptance:trace"),
        dependency_ids=(),
        active_overlap=False,
        acceptance_complete=True,
        append_only_safe=True,
        authority_granted=True,
        evidence_complete=True,
        complete=False,
    )


def _snapshot(
    kind: DesignUnitKind = DesignUnitKind.CHILD_TASK,
    *,
    candidates: tuple[DesignCandidate, ...] | None = None,
) -> DesignSnapshot:
    return DesignSnapshot(
        source_id="test.publication",
        repository="alexanderholman/AgentFramework",
        base_ref="master",
        head_sha="a" * 40,
        captured_at=_CAPTURED_AT,
        candidates=(_candidate(kind),) if candidates is None else candidates,
    )


def _prepared(kind: DesignUnitKind = DesignUnitKind.CHILD_TASK) -> PreparedDesignPlan:
    operation = SelfDesignOperation(
        PublicationEvidenceProvider(kind),
        DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
        lambda: _CAPTURED_AT + timedelta(seconds=1),
    )
    return operation.prepare()


def _stopped() -> PreparedDesignPlan:
    operation = SelfDesignOperation(
        EmptyPublicationEvidenceProvider(),
        DesignPolicy(max_snapshot_age_seconds=60, tie_break_by_id=True),
        lambda: _CAPTURED_AT + timedelta(seconds=1),
    )
    return operation.prepare()


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


def _recorded(tmp_path: Path, prepared: PreparedDesignPlan) -> DesignRecordResult:
    coordination = _coordination(tmp_path)
    record_authority = DesignRecordAuthority(
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
    request = DesignRecordingRequest(
        prepared=prepared,
        authority=record_authority,
        coordination=coordination,
        observed_at=_CAPTURED_AT + timedelta(seconds=2),
    )
    record = DesignPlanRecord.from_request(request)
    record_path = (
        Path(coordination.manifest.repository.git_common_dir)
        / "af"
        / "self-design"
        / "records"
        / f"{prepared.plan.digest.removeprefix('sha256:')}.json"
    )
    return DesignRecordResult(
        status=DesignRecordStatus.RECORDED,
        record=record,
        record_path=str(record_path),
    )


def _draft(prepared: PreparedDesignPlan) -> DesignPublicationDraft:
    plan = prepared.plan
    assert plan.selected_candidate_id is not None
    assert plan.unit_kind is not None
    assert plan.objective_id is not None
    if plan.unit_kind is DesignUnitKind.CHILD_TASK:
        return DesignPublicationDraft(
            effect=DesignPublicationEffect.CREATE_ISSUE,
            repository=prepared.snapshot.repository,
            selected_candidate_id=plan.selected_candidate_id,
            unit_kind=plan.unit_kind,
            objective_id=plan.objective_id,
            acceptance_clause_ids=plan.acceptance_clause_ids,
            target_issue_number=None,
            expected_target_revision=None,
            title="Implement the admitted self-design child task",
            body="Objective and acceptance trace are carried by typed fields.",
        )
    return DesignPublicationDraft(
        effect=DesignPublicationEffect.ADD_ISSUE_COMMENT,
        repository=prepared.snapshot.repository,
        selected_candidate_id=plan.selected_candidate_id,
        unit_kind=plan.unit_kind,
        objective_id=plan.objective_id,
        acceptance_clause_ids=plan.acceptance_clause_ids,
        target_issue_number=124,
        expected_target_revision="sha256:" + "c" * 64,
        title=None,
        body="Append-only design evidence for the admitted bounded unit.",
    )


def _authority(
    prepared: PreparedDesignPlan,
    recorded: DesignRecordResult,
    draft: DesignPublicationDraft,
) -> DesignPublicationAuthority:
    return DesignPublicationAuthority(
        authority_id="test:publish-124",
        repository=prepared.snapshot.repository,
        base_ref=prepared.snapshot.base_ref,
        head_sha=prepared.snapshot.head_sha,
        snapshot_digest=prepared.snapshot.digest,
        plan_digest=prepared.plan.digest,
        record_digest=recorded.record.digest,
        record_path=recorded.record_path,
        draft_digest=draft.digest,
        issued_at=_CAPTURED_AT,
        expires_at=_CAPTURED_AT + timedelta(minutes=5),
        allowed_effects=(draft.effect,),
    )


def _request(
    tmp_path: Path,
    kind: DesignUnitKind = DesignUnitKind.CHILD_TASK,
) -> DesignPublicationRequest:
    prepared = _prepared(kind)
    recorded = _recorded(tmp_path, prepared)
    draft = _draft(prepared)
    return DesignPublicationRequest(
        prepared=prepared,
        recorded=recorded,
        draft=draft,
        authority=_authority(prepared, recorded, draft),
        observed_at=_OBSERVED_AT,
    )


def test_publication_draft_is_canonical_and_append_only() -> None:
    prepared = _prepared()
    draft = _draft(prepared)
    reversed_draft = replace(
        draft,
        acceptance_clause_ids=tuple(reversed(draft.acceptance_clause_ids)),
    )

    assert draft.canonical_json() == reversed_draft.canonical_json()
    assert json.loads(draft.canonical_json())["effect"] == "create_issue"
    assert draft.digest == reversed_draft.digest

    comment = _draft(_prepared(DesignUnitKind.ACCEPTANCE_REPAIR))
    assert comment.effect is DesignPublicationEffect.ADD_ISSUE_COMMENT
    assert comment.title is None


def test_publication_draft_rejects_replace_style_or_mismatched_effects() -> None:
    child = _draft(_prepared())
    repair = _draft(_prepared(DesignUnitKind.ACCEPTANCE_REPAIR))

    with pytest.raises(ValueError, match="child tasks must create an issue"):
        replace(
            child,
            effect=DesignPublicationEffect.ADD_ISSUE_COMMENT,
            target_issue_number=124,
            expected_target_revision="sha256:" + "c" * 64,
            title=None,
        )
    with pytest.raises(ValueError, match="cannot replace a title"):
        replace(repair, title="Replacement title")


def test_publication_request_is_exactly_bound(tmp_path: Path) -> None:
    request = _request(tmp_path)

    assert request.authority.allowed_effects == (request.draft.effect,)
    assert request.authority.record_digest == request.recorded.record.digest
    assert request.authority.draft_digest == request.draft.digest


@pytest.mark.parametrize(
    "replacement",
    [
        {"repository": "other/Repository"},
        {"base_ref": "other"},
        {"head_sha": "d" * 40},
        {"snapshot_digest": "sha256:" + "d" * 64},
        {"plan_digest": "sha256:" + "e" * 64},
        {"record_digest": "sha256:" + "f" * 64},
        {"record_path": "/different/record.json"},
        {"draft_digest": "sha256:" + "1" * 64},
    ],
)
def test_publication_request_rejects_authority_binding_drift(
    tmp_path: Path,
    replacement: dict[str, object],
) -> None:
    request = _request(tmp_path)

    with pytest.raises(DesignPublicationError, match="evidence binding is invalid"):
        replace(request, authority=replace(request.authority, **replacement))


def test_publication_request_rejects_trace_or_local_record_drift(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(DesignPublicationError, match="traceability is invalid"):
        replace(
            request,
            draft=replace(request.draft, objective_id="objective:other"),
        )

    other_prepared = _prepared(DesignUnitKind.ACCEPTANCE_REPAIR)
    with pytest.raises(DesignPublicationError, match="local record is invalid"):
        replace(
            request,
            recorded=replace(
                request.recorded,
                record=replace(request.recorded.record, prepared=other_prepared),
            ),
        )


def test_stop_plan_cannot_be_published(tmp_path: Path) -> None:
    stopped = _stopped()
    recorded = _recorded(tmp_path, stopped)
    proposed = _prepared()
    draft = _draft(proposed)
    authority = _authority(stopped, recorded, draft)

    with pytest.raises(DesignPublicationError, match="stop plans cannot be published"):
        DesignPublicationRequest(
            prepared=stopped,
            recorded=recorded,
            draft=draft,
            authority=authority,
            observed_at=_OBSERVED_AT,
        )


class RecordingPublisher:
    def __init__(self) -> None:
        self.requests: list[DesignPublicationRequest] = []

    def publish(self, request: DesignPublicationRequest) -> DesignPublicationReceipt:
        self.requests.append(request)
        return DesignPublicationReceipt(
            status=DesignPublicationStatus.PUBLISHED,
            effect=request.draft.effect,
            repository=request.draft.repository,
            issue_number=request.draft.target_issue_number or 146,
            target_revision="sha256:" + "9" * 64,
            draft_digest=request.draft.digest,
        )


def test_controller_invokes_admitted_publisher_once(tmp_path: Path) -> None:
    request = _request(tmp_path)
    publisher = RecordingPublisher()
    controller = SelfDesignPublicationController(publisher, lambda: _OBSERVED_AT)

    receipt = controller.publish(
        request.prepared,
        request.recorded,
        request.draft,
        request.authority,
    )

    assert len(publisher.requests) == 1
    assert publisher.requests[0].authority is request.authority
    assert receipt.issue_number == 146
    assert receipt.to_dict()["status"] == "published"


def test_controller_fails_before_external_effect_when_authority_is_expired(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    publisher = RecordingPublisher()
    controller = SelfDesignPublicationController(
        publisher,
        lambda: request.authority.expires_at + timedelta(microseconds=1),
    )

    with pytest.raises(DesignPublicationError, match="not currently valid"):
        controller.publish(
            request.prepared,
            request.recorded,
            request.draft,
            request.authority,
        )

    assert publisher.requests == []


class MisboundPublisher:
    def publish(self, request: DesignPublicationRequest) -> DesignPublicationReceipt:
        return DesignPublicationReceipt(
            status=DesignPublicationStatus.PUBLISHED,
            effect=request.draft.effect,
            repository="other/Repository",
            issue_number=request.draft.target_issue_number or 146,
            target_revision="sha256:" + "9" * 64,
            draft_digest=request.draft.digest,
        )


def test_controller_rejects_misbound_publisher_receipt(tmp_path: Path) -> None:
    request = _request(tmp_path)
    controller = SelfDesignPublicationController(MisboundPublisher(), lambda: _OBSERVED_AT)

    with pytest.raises(DesignPublicationError, match="receipt binding is invalid"):
        controller.publish(
            request.prepared,
            request.recorded,
            request.draft,
            request.authority,
        )


def test_publication_authority_is_single_effect_and_short_lived(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(ValueError, match="grants unsupported effects"):
        replace(request.authority, allowed_effects=())
    with pytest.raises(ValueError, match="grants unsupported effects"):
        replace(
            request.authority,
            allowed_effects=(
                DesignPublicationEffect.CREATE_ISSUE,
                DesignPublicationEffect.ADD_ISSUE_COMMENT,
            ),
        )
    with pytest.raises(ValueError, match="lifetime is too long"):
        replace(
            request.authority,
            expires_at=request.authority.issued_at + timedelta(hours=2),
        )
