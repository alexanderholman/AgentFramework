from __future__ import annotations

from dataclasses import replace
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
    GitHubIssueState,
    GitHubIssueStatus,
)

_REPOSITORY = "alexanderholman/AgentFramework"
_NOW = datetime(2026, 7, 25, 5, 45, tzinfo=UTC)


class ClientShape:
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
        raise AssertionError((repository, issue_number, body, expected_revision, idempotency_key))


def _issue(
    *,
    issue_number: int = 124,
    title: str = "Governed self-design",
    body: str = "Issue body",
    status: GitHubIssueStatus = GitHubIssueStatus.OPEN,
    updated_at: datetime = _NOW,
    comment_count: int = 2,
) -> GitHubIssueState:
    return GitHubIssueState(
        repository=_REPOSITORY,
        issue_number=issue_number,
        title=title,
        body=body,
        status=status,
        updated_at=updated_at,
        comment_count=comment_count,
    )


def _create_draft() -> DesignPublicationDraft:
    return DesignPublicationDraft(
        effect=DesignPublicationEffect.CREATE_ISSUE,
        repository=_REPOSITORY,
        selected_candidate_id="issue:124",
        unit_kind=DesignUnitKind.CHILD_TASK,
        objective_id="objective:self-design",
        acceptance_clause_ids=("acceptance:publish",),
        target_issue_number=None,
        expected_target_revision=None,
        title="Implement the admitted child task",
        body="Exact admitted issue body.",
    )


def _comment_draft(previous: GitHubIssueState) -> DesignPublicationDraft:
    return DesignPublicationDraft(
        effect=DesignPublicationEffect.ADD_ISSUE_COMMENT,
        repository=_REPOSITORY,
        selected_candidate_id="issue:124",
        unit_kind=DesignUnitKind.ACCEPTANCE_REPAIR,
        objective_id="objective:self-design",
        acceptance_clause_ids=("acceptance:publish",),
        target_issue_number=previous.issue_number,
        expected_target_revision=previous.revision,
        title=None,
        body="Append-only design evidence.",
    )


def test_creation_result_rejects_external_state_drift() -> None:
    publisher = GitHubDesignPublisher(ClientShape(), _REPOSITORY)
    draft = _create_draft()
    assert draft.title is not None
    created = _issue(
        issue_number=147,
        title=draft.title,
        body=draft.body,
        comment_count=0,
    )

    for published in (
        replace(created, title="Changed title"),
        replace(created, body="Changed body"),
        replace(created, status=GitHubIssueStatus.CLOSED),
        replace(created, comment_count=1),
    ):
        with pytest.raises(DesignPublicationError, match="creation result is inconsistent"):
            publisher._verify_published_result(draft, published, None)


def test_comment_result_rejects_non_append_only_transition() -> None:
    publisher = GitHubDesignPublisher(ClientShape(), _REPOSITORY)
    previous = _issue()
    draft = _comment_draft(previous)
    published = replace(
        previous,
        updated_at=previous.updated_at + timedelta(seconds=1),
        comment_count=previous.comment_count + 1,
    )

    for drifted in (
        replace(published, title="Changed title"),
        replace(published, body="Changed body"),
        replace(published, status=GitHubIssueStatus.CLOSED),
        replace(published, comment_count=previous.comment_count),
        replace(published, updated_at=previous.updated_at - timedelta(seconds=1)),
    ):
        with pytest.raises(DesignPublicationError, match="comment result is not append-only"):
            publisher._verify_published_result(draft, drifted, previous)
