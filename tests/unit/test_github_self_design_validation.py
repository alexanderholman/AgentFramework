from __future__ import annotations

from datetime import UTC, datetime

import pytest

from af.infrastructure.github_self_design import (
    GitHubDesignPublisher,
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
    }


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


class ValidClientShape:
    def get_issue(self, repository: str, issue_number: int) -> GitHubIssueState:
        raise AssertionError((repository, issue_number))

    def create_issue(
        self,
        repository: str,
        *,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> GitHubIssueState:
        raise AssertionError((repository, title, body, idempotency_key))

    def add_issue_comment(
        self,
        repository: str,
        issue_number: int,
        *,
        body: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> GitHubIssueState:
        raise AssertionError(
            (repository, issue_number, body, expected_revision, idempotency_key)
        )


def test_publisher_rejects_incomplete_client_boundary() -> None:
    with pytest.raises(TypeError, match="issue client is invalid"):
        GitHubDesignPublisher(object(), _REPOSITORY)  # type: ignore[arg-type]


def test_publisher_rejects_invalid_repository_boundary() -> None:
    with pytest.raises(ValueError, match="repository identity is invalid"):
        GitHubDesignPublisher(ValidClientShape(), "invalid")
