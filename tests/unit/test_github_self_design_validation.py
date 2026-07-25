from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest
from af.infrastructure.github_self_design import (
    GitHubCommentPublication,
    GitHubDesignPublisher,
    GitHubIssueComment,
    GitHubIssueCreation,
    GitHubIssueState,
    GitHubIssueStatus,
)

_REPOSITORY = "alexanderholman/AgentFramework"
_UPDATED_AT = datetime(2026, 7, 25, 4, 30, tzinfo=UTC)


def _issue_values() -> dict[str, object]:
    return {
        "repository": _REPOSITORY,
        "issue_number": 124,
        "title": "Governed self-design",
        "body": "Issue body",
        "status": GitHubIssueStatus.OPEN,
        "updated_at": _UPDATED_AT,
        "comment_count": 2,
        "latest_comment_id": 8999,
    }


def _comment_values() -> dict[str, object]:
    return {
        "repository": _REPOSITORY,
        "issue_number": 124,
        "comment_id": 9001,
        "body": "Exact append-only evidence.",
        "updated_at": _UPDATED_AT,
    }


def test_creation_evidence_is_canonical_and_strictly_bound() -> None:
    issue = GitHubIssueState(**_issue_values())  # type: ignore[arg-type]
    creation = GitHubIssueCreation(
        issue=issue,
        draft_digest="sha256:" + "a" * 64,
        created_at=_UPDATED_AT - timedelta(seconds=1),
    )
    equivalent = replace(
        creation,
        created_at=datetime(
            2026,
            7,
            25,
            5,
            29,
            59,
            tzinfo=timezone(timedelta(hours=1)),
        ),
    )

    assert creation.canonical_json() == equivalent.canonical_json()
    assert creation.revision == equivalent.revision
    assert creation.revision.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    (
        ("issue", object(), TypeError, "creation state is invalid"),
        ("draft_digest", "invalid", ValueError, "draft digest is invalid"),
        (
            "created_at",
            datetime(2026, 7, 25, 4, 29),
            ValueError,
            "creation time must be timezone aware",
        ),
        (
            "created_at",
            _UPDATED_AT + timedelta(seconds=1),
            ValueError,
            "creation transition is invalid",
        ),
    ),
)
def test_creation_evidence_rejects_invalid_values(
    field: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    values: dict[str, object] = {
        "issue": GitHubIssueState(**_issue_values()),  # type: ignore[arg-type]
        "draft_digest": "sha256:" + "a" * 64,
        "created_at": _UPDATED_AT - timedelta(seconds=1),
    }
    values[field] = value

    with pytest.raises(error_type, match=message):
        GitHubIssueCreation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    (
        ("repository", "invalid", ValueError, "repository identity"),
        ("issue_number", 0, ValueError, "issue number"),
        ("title", " ", ValueError, "issue title"),
        ("body", "contains\x00nul", ValueError, "issue body"),
        ("status", "open", TypeError, "issue status"),
        ("updated_at", datetime(2026, 7, 25, 4, 30), ValueError, "timezone aware"),
        ("comment_count", -1, ValueError, "comment count"),
        ("latest_comment_id", 0, ValueError, "latest comment identity"),
    ),
)
def test_issue_state_rejects_invalid_external_evidence(
    field: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    values = _issue_values()
    values[field] = value

    with pytest.raises(error_type, match=message):
        GitHubIssueState(**values)  # type: ignore[arg-type]


def test_issue_state_rejects_comment_count_watermark_mismatch() -> None:
    values = _issue_values()
    values.update(comment_count=0, latest_comment_id=8999)
    with pytest.raises(ValueError, match="watermark is inconsistent"):
        GitHubIssueState(**values)  # type: ignore[arg-type]

    values.update(comment_count=1, latest_comment_id=None)
    with pytest.raises(ValueError, match="watermark is inconsistent"):
        GitHubIssueState(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "error_type", "message"),
    (
        ("repository", "invalid", ValueError, "repository identity"),
        ("issue_number", 0, ValueError, "issue number"),
        ("comment_id", 0, ValueError, "comment identity"),
        ("body", "contains\x00nul", ValueError, "comment body"),
        ("updated_at", datetime(2026, 7, 25, 4, 30), ValueError, "timezone aware"),
    ),
)
def test_comment_rejects_invalid_external_evidence(
    field: str,
    value: object,
    error_type: type[Exception],
    message: str,
) -> None:
    values = _comment_values()
    values[field] = value

    with pytest.raises(error_type, match=message):
        GitHubIssueComment(**values)  # type: ignore[arg-type]


def test_comment_revision_is_canonical_across_equivalent_timezones() -> None:
    comment = GitHubIssueComment(**_comment_values())  # type: ignore[arg-type]
    equivalent = replace(
        comment,
        updated_at=datetime(
            2026,
            7,
            25,
            5,
            30,
            tzinfo=timezone(timedelta(hours=1)),
        ),
    )

    assert comment.canonical_json() == equivalent.canonical_json()
    assert comment.revision == equivalent.revision
    assert comment.revision.startswith("sha256:")


def test_comment_publication_rejects_invalid_or_misbound_evidence() -> None:
    issue = GitHubIssueState(**_issue_values())  # type: ignore[arg-type]
    comment = GitHubIssueComment(**_comment_values())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="issue state is invalid"):
        GitHubCommentPublication(issue=object(), comment=comment)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="evidence is invalid"):
        GitHubCommentPublication(issue=issue, comment=object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="evidence is misbound"):
        GitHubCommentPublication(
            issue=issue,
            comment=replace(comment, issue_number=125),
        )


class ValidClientShape:
    def get_issue(self, repository: str, issue_number: int) -> GitHubIssueState:
        raise AssertionError((repository, issue_number))

    def get_issue_creation(
        self,
        repository: str,
        issue_number: int,
    ) -> GitHubIssueCreation:
        raise AssertionError((repository, issue_number))

    def create_issue(
        self,
        repository: str,
        *,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> GitHubIssueCreation:
        raise AssertionError((repository, title, body, idempotency_key))

    def add_issue_comment(
        self,
        repository: str,
        issue_number: int,
        *,
        body: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> GitHubCommentPublication:
        raise AssertionError((repository, issue_number, body, expected_revision, idempotency_key))


def test_publisher_rejects_incomplete_client_boundary() -> None:
    with pytest.raises(TypeError, match="issue client is invalid"):
        GitHubDesignPublisher(object(), _REPOSITORY)  # type: ignore[arg-type]


def test_publisher_rejects_invalid_repository_boundary() -> None:
    with pytest.raises(ValueError, match="repository identity is invalid"):
        GitHubDesignPublisher(ValidClientShape(), "invalid")  # type: ignore[arg-type]
