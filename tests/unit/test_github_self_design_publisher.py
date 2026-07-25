from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

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
    DesignPublicationRequest,
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
from af.infrastructure.github_self_design import (
    GitHubCommentPublication,
    GitHubDesignPublisher,
    GitHubIssueComment,
    GitHubIssueState,
    GitHubIssueStatus,
)

_CAPTURED_AT = datetime(2026, 7, 25, 4, 30, tzinfo=UTC)
_OBSERVED_AT = _CAPTURED_AT + timedelta(seconds=3)
_REPOSITORY = "alexanderholman/AgentFramework"


class PublicationEvidenceProvider:
    def __init__(self, kind: DesignUnitKind) -> None:
        self.kind = kind

    def capture_snapshot(self) -> DesignSnapshot:
        return DesignSnapshot(
            source_id="test.github-publication",
            repository=_REPOSITORY,
            base_ref="master",
            head_sha="a" * 40,
            captured_at=_CAPTURED_AT,
            candidates=(
                DesignCandidate(
                    candidate_id="issue:124",
                    revision="sha256:" + "b" * 64,
                    kind=self.kind,
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
                ),
            ),
        )


def _prepared(kind: DesignUnitKind) -> PreparedDesignPlan:
    return SelfDesignOperation(
        PublicationEvidenceProvider(kind),
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


def _recorded(tmp_path: Path, prepared: PreparedDesignPlan) -> DesignRecordResult:
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
    record_path.parent.mkdir(parents=True)
    record_path.write_bytes(record.canonical_bytes())
    return DesignRecordResult(
        status=DesignRecordStatus.RECORDED,
        record=record,
        record_path=str(record_path),
    )


def _draft(
    prepared: PreparedDesignPlan,
    *,
    expected_target_revision: str | None = None,
) -> DesignPublicationDraft:
    plan = prepared.plan
    assert plan.selected_candidate_id is not None
    assert plan.unit_kind is not None
    assert plan.objective_id is not None
    if plan.unit_kind is DesignUnitKind.CHILD_TASK:
        return DesignPublicationDraft(
            effect=DesignPublicationEffect.CREATE_ISSUE,
            repository=_REPOSITORY,
            selected_candidate_id=plan.selected_candidate_id,
            unit_kind=plan.unit_kind,
            objective_id=plan.objective_id,
            acceptance_clause_ids=plan.acceptance_clause_ids,
            target_issue_number=None,
            expected_target_revision=None,
            title="Implement the admitted self-design child task",
            body="Objective and acceptance trace are carried by typed fields.",
        )
    assert expected_target_revision is not None
    return DesignPublicationDraft(
        effect=DesignPublicationEffect.ADD_ISSUE_COMMENT,
        repository=_REPOSITORY,
        selected_candidate_id=plan.selected_candidate_id,
        unit_kind=plan.unit_kind,
        objective_id=plan.objective_id,
        acceptance_clause_ids=plan.acceptance_clause_ids,
        target_issue_number=124,
        expected_target_revision=expected_target_revision,
        title=None,
        body="Append-only design evidence for the admitted bounded unit.",
    )


def _request(
    tmp_path: Path,
    kind: DesignUnitKind,
    *,
    expected_target_revision: str | None = None,
) -> DesignPublicationRequest:
    prepared = _prepared(kind)
    recorded = _recorded(tmp_path, prepared)
    draft = _draft(prepared, expected_target_revision=expected_target_revision)
    authority = DesignPublicationAuthority(
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
    return DesignPublicationRequest(
        prepared=prepared,
        recorded=recorded,
        draft=draft,
        authority=authority,
        observed_at=_OBSERVED_AT,
    )


def _issue(
    *,
    issue_number: int = 124,
    comment_count: int = 2,
    updated_at: datetime = _OBSERVED_AT,
    latest_comment_id: int | None = 8999,
) -> GitHubIssueState:
    return GitHubIssueState(
        repository=_REPOSITORY,
        issue_number=issue_number,
        title="Governed self-design",
        body="Issue body",
        status=GitHubIssueStatus.OPEN,
        updated_at=updated_at,
        comment_count=comment_count,
        latest_comment_id=latest_comment_id,
    )


class RecordingGitHubClient:
    def __init__(self, current: GitHubIssueState | None = None) -> None:
        self.current = current or _issue()
        self.comment: GitHubIssueComment | None = None
        self.calls: list[tuple[object, ...]] = []

    def get_issue(self, repository: str, issue_number: int) -> GitHubIssueState:
        self.calls.append(("get", repository, issue_number))
        return self.current

    def get_issue_comment(
        self,
        repository: str,
        issue_number: int,
        comment_id: int,
    ) -> GitHubIssueComment:
        self.calls.append(("get-comment", repository, issue_number, comment_id))
        assert self.comment is not None
        return self.comment

    def create_issue(
        self,
        repository: str,
        *,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> GitHubIssueState:
        self.calls.append(("create", repository, title, body, idempotency_key))
        self.current = GitHubIssueState(
            repository=repository,
            issue_number=147,
            title=title,
            body=body,
            status=GitHubIssueStatus.OPEN,
            updated_at=_OBSERVED_AT + timedelta(seconds=1),
            comment_count=0,
        )
        return self.current

    def add_issue_comment(
        self,
        repository: str,
        issue_number: int,
        *,
        body: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> GitHubCommentPublication:
        self.calls.append(
            (
                "comment",
                repository,
                issue_number,
                body,
                expected_revision,
                idempotency_key,
            )
        )
        updated_at = self.current.updated_at + timedelta(seconds=1)
        self.current = replace(
            self.current,
            updated_at=updated_at,
            comment_count=self.current.comment_count + 1,
            latest_comment_id=9001,
        )
        self.comment = GitHubIssueComment(
            repository=repository,
            issue_number=issue_number,
            comment_id=9001,
            body=body,
            updated_at=updated_at,
        )
        return GitHubCommentPublication(issue=self.current, comment=self.comment)


def test_issue_revision_is_canonical_and_evidence_complete() -> None:
    issue = _issue(updated_at=datetime(2026, 7, 25, 5, 30, tzinfo=UTC))
    equivalent = replace(
        issue,
        updated_at=datetime(
            2026,
            7,
            25,
            6,
            30,
            tzinfo=timezone(timedelta(hours=1)),
        ),
    )

    assert issue.canonical_json() == equivalent.canonical_json()
    assert issue.revision == equivalent.revision
    assert issue.revision.startswith("sha256:")


def test_issue_comment_watermark_is_count_consistent_and_revision_bound() -> None:
    issue = _issue()

    assert replace(issue, latest_comment_id=8998).revision != issue.revision
    with pytest.raises(ValueError, match="watermark is inconsistent"):
        _issue(comment_count=0, latest_comment_id=8999)
    with pytest.raises(ValueError, match="watermark is inconsistent"):
        _issue(comment_count=1, latest_comment_id=None)
    with pytest.raises(ValueError, match="latest comment identity is invalid"):
        _issue(comment_count=1, latest_comment_id=0)


def test_publisher_creates_one_issue_after_exact_record_revalidation(tmp_path: Path) -> None:
    request = _request(tmp_path, DesignUnitKind.CHILD_TASK)
    client = RecordingGitHubClient()

    receipt = GitHubDesignPublisher(client, _REPOSITORY).publish(request)

    assert receipt.issue_number == 147
    assert receipt.draft_digest == request.draft.digest
    assert client.calls == [
        (
            "create",
            _REPOSITORY,
            request.draft.title,
            request.draft.body,
            request.draft.digest,
        ),
        ("get", _REPOSITORY, 147),
    ]


def test_comment_rechecks_target_and_passes_exact_precondition(tmp_path: Path) -> None:
    current = _issue()
    request = _request(
        tmp_path,
        DesignUnitKind.ACCEPTANCE_REPAIR,
        expected_target_revision=current.revision,
    )
    client = RecordingGitHubClient(current)

    receipt = GitHubDesignPublisher(client, _REPOSITORY).publish(request)

    assert receipt.issue_number == 124
    assert receipt.target_revision != current.revision
    assert client.calls == [
        ("get", _REPOSITORY, 124),
        (
            "comment",
            _REPOSITORY,
            124,
            request.draft.body,
            current.revision,
            request.draft.digest,
        ),
        ("get-comment", _REPOSITORY, 124, 9001),
        ("get", _REPOSITORY, 124),
    ]


def test_comment_rejects_wrong_returned_body(tmp_path: Path) -> None:
    current = _issue()
    request = _request(
        tmp_path,
        DesignUnitKind.ACCEPTANCE_REPAIR,
        expected_target_revision=current.revision,
    )
    client = RecordingGitHubClient(current)
    original_add = client.add_issue_comment

    def wrong_body_comment(
        repository: str,
        issue_number: int,
        *,
        body: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> GitHubCommentPublication:
        publication = original_add(
            repository,
            issue_number,
            body=body,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        return replace(
            publication,
            comment=replace(publication.comment, body="different comment"),
        )

    client.add_issue_comment = wrong_body_comment  # type: ignore[method-assign]

    with pytest.raises(DesignPublicationError, match="comment result is inconsistent"):
        GitHubDesignPublisher(client, _REPOSITORY).publish(request)


def test_comment_rejects_equal_timestamp_preexisting_comment_alias(
    tmp_path: Path,
) -> None:
    current = _issue()
    request = _request(
        tmp_path,
        DesignUnitKind.ACCEPTANCE_REPAIR,
        expected_target_revision=current.revision,
    )
    client = RecordingGitHubClient(current)
    original_add = client.add_issue_comment

    def aliased_comment(
        repository: str,
        issue_number: int,
        *,
        body: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> GitHubCommentPublication:
        publication = original_add(
            repository,
            issue_number,
            body="different appended comment",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )
        previous_comment = GitHubIssueComment(
            repository=repository,
            issue_number=issue_number,
            comment_id=8999,
            body=body,
            updated_at=current.updated_at,
        )
        client.comment = previous_comment
        return replace(publication, comment=previous_comment)

    client.add_issue_comment = aliased_comment  # type: ignore[method-assign]

    with pytest.raises(DesignPublicationError, match="comment result is inconsistent"):
        GitHubDesignPublisher(client, _REPOSITORY).publish(request)


def test_comment_rejects_post_publication_readback_drift(tmp_path: Path) -> None:
    current = _issue()
    request = _request(
        tmp_path,
        DesignUnitKind.ACCEPTANCE_REPAIR,
        expected_target_revision=current.revision,
    )
    client = RecordingGitHubClient(current)
    original_get = client.get_issue_comment

    def drifted_comment(
        repository: str,
        issue_number: int,
        comment_id: int,
    ) -> GitHubIssueComment:
        comment = original_get(repository, issue_number, comment_id)
        return replace(comment, body="drifted comment")

    client.get_issue_comment = drifted_comment  # type: ignore[method-assign]

    with pytest.raises(DesignPublicationError, match="comment readback drifted"):
        GitHubDesignPublisher(client, _REPOSITORY).publish(request)


def test_stale_target_stops_before_comment_effect(tmp_path: Path) -> None:
    current = _issue()
    request = _request(
        tmp_path,
        DesignUnitKind.ACCEPTANCE_REPAIR,
        expected_target_revision="sha256:" + "f" * 64,
    )
    client = RecordingGitHubClient(current)

    with pytest.raises(DesignPublicationError, match="target revision is stale"):
        GitHubDesignPublisher(client, _REPOSITORY).publish(request)

    assert client.calls == [("get", _REPOSITORY, 124)]


def test_changed_local_record_stops_before_any_github_call(tmp_path: Path) -> None:
    request = _request(tmp_path, DesignUnitKind.CHILD_TASK)
    Path(request.recorded.record_path).write_text("changed\n", encoding="utf-8")
    client = RecordingGitHubClient()

    with pytest.raises(DesignPublicationError, match="local record has changed"):
        GitHubDesignPublisher(client, _REPOSITORY).publish(request)

    assert client.calls == []


def test_symlinked_local_record_stops_before_any_github_call(tmp_path: Path) -> None:
    request = _request(tmp_path, DesignUnitKind.CHILD_TASK)
    record_path = Path(request.recorded.record_path)
    replacement = record_path.with_name("replacement.json")
    replacement.write_bytes(request.recorded.record.canonical_bytes())
    record_path.unlink()
    try:
        record_path.symlink_to(replacement)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")
    client = RecordingGitHubClient()

    with pytest.raises(DesignPublicationError, match="local record is invalid"):
        GitHubDesignPublisher(client, _REPOSITORY).publish(request)

    assert client.calls == []


def test_misbound_client_result_is_rejected(tmp_path: Path) -> None:
    request = _request(tmp_path, DesignUnitKind.CHILD_TASK)
    client = RecordingGitHubClient()

    def misbound_create(
        repository: str,
        *,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> GitHubIssueState:
        del repository, idempotency_key
        return GitHubIssueState(
            repository="other/Repository",
            issue_number=147,
            title=title,
            body=body,
            status=GitHubIssueStatus.OPEN,
            updated_at=_OBSERVED_AT,
            comment_count=0,
        )

    client.create_issue = misbound_create  # type: ignore[method-assign]

    with pytest.raises(DesignPublicationError, match="result is misbound"):
        GitHubDesignPublisher(client, _REPOSITORY).publish(request)
