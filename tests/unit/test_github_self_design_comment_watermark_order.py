from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from af.framework.self_design import DesignUnitKind
from af.framework.self_design_publication import (
    DesignPublicationDraft,
    DesignPublicationEffect,
    DesignPublicationError,
)
from af.infrastructure.github_self_design import (
    GitHubDesignPublisher,
    GitHubIssueComment,
    GitHubIssueState,
    GitHubIssueStatus,
)

_REPOSITORY = "alexanderholman/AgentFramework"
_NOW = datetime(2026, 7, 25, 16, 0, tzinfo=UTC)


class _ClientShape:
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
    ) -> object:
        raise AssertionError((repository, issue_number, body, expected_revision, idempotency_key))


def _issue(*, comment_count: int, latest_comment_id: int, updated_at: datetime) -> GitHubIssueState:
    return GitHubIssueState(
        repository=_REPOSITORY,
        issue_number=124,
        title="Governed self-design",
        body="Issue body",
        status=GitHubIssueStatus.OPEN,
        updated_at=updated_at,
        comment_count=comment_count,
        latest_comment_id=latest_comment_id,
    )


def test_comment_publication_rejects_a_regressing_comment_watermark() -> None:
    previous = _issue(comment_count=2, latest_comment_id=9001, updated_at=_NOW)
    published = _issue(
        comment_count=3,
        latest_comment_id=8999,
        updated_at=_NOW + timedelta(seconds=1),
    )
    draft = DesignPublicationDraft(
        effect=DesignPublicationEffect.ADD_ISSUE_COMMENT,
        repository=_REPOSITORY,
        selected_candidate_id="issue:124",
        unit_kind=DesignUnitKind.ACCEPTANCE_REPAIR,
        objective_id="objective:self-design",
        acceptance_clause_ids=("acceptance:publish",),
        target_issue_number=124,
        expected_target_revision=previous.revision,
        title=None,
        body="Append-only design evidence.",
    )
    comment = GitHubIssueComment(
        repository=_REPOSITORY,
        issue_number=124,
        comment_id=8999,
        body=draft.body,
        updated_at=published.updated_at,
    )
    publisher = GitHubDesignPublisher(_ClientShape(), _REPOSITORY)

    with pytest.raises(DesignPublicationError, match="comment result is not append-only"):
        publisher._verify_published_result(draft, published, previous)

    with pytest.raises(DesignPublicationError, match="comment result is inconsistent"):
        publisher._verify_published_comment(draft, previous, published, comment)
