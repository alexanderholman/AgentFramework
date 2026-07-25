"""GitHub publication adapter for one exact governed self-design effect."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from af.framework.self_design_publication import (
    DesignPublicationDraft,
    DesignPublicationEffect,
    DesignPublicationError,
    DesignPublicationReceipt,
    DesignPublicationRequest,
    DesignPublicationStatus,
)
from af.framework.self_design_recording import SELF_DESIGN_RECORD_MAX_BYTES

_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_MAX_ISSUE_BODY_BYTES = 1024 * 1024
_MAX_ISSUE_TITLE_LENGTH = 256


class GitHubIssueStatus(StrEnum):
    """Issue states used by the narrow publication adapter boundary."""

    OPEN = "open"
    CLOSED = "closed"


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("GitHub issue update time must be timezone aware")
    return value.astimezone(UTC)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class GitHubIssueState:
    """Canonical target state used for exact revision checks and receipts."""

    repository: str
    issue_number: int
    title: str
    body: str
    status: GitHubIssueStatus
    updated_at: datetime
    comment_count: int

    def __post_init__(self) -> None:
        if type(self.repository) is not str or _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("GitHub issue repository identity is invalid")
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("GitHub issue number is invalid")
        if (
            type(self.title) is not str
            or not self.title.strip()
            or len(self.title) > _MAX_ISSUE_TITLE_LENGTH
            or "\x00" in self.title
        ):
            raise ValueError("GitHub issue title is invalid")
        if (
            type(self.body) is not str
            or "\x00" in self.body
            or len(self.body.encode("utf-8")) > _MAX_ISSUE_BODY_BYTES
        ):
            raise ValueError("GitHub issue body is invalid")
        if not isinstance(self.status, GitHubIssueStatus):
            raise TypeError("GitHub issue status is invalid")
        object.__setattr__(self, "updated_at", _aware_utc(self.updated_at))
        if type(self.comment_count) is not int or self.comment_count < 0:
            raise ValueError("GitHub issue comment count is invalid")

    def canonical_json(self) -> str:
        """Return the deterministic external target representation."""
        return _canonical_json(
            {
                "body": self.body,
                "comment_count": self.comment_count,
                "issue_number": self.issue_number,
                "repository": self.repository,
                "status": self.status.value,
                "title": self.title,
                "updated_at": self.updated_at.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                ),
            }
        )

    @property
    def revision(self) -> str:
        """Return the exact digest used as the target revision precondition."""
        return _digest(self.canonical_json())


class GitHubIssueClient(Protocol):
    """Narrow GitHub issue client admitted beneath the publication adapter."""

    def get_issue(self, repository: str, issue_number: int) -> GitHubIssueState:
        """Return the current canonical issue state."""
        ...

    def create_issue(
        self,
        repository: str,
        *,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> GitHubIssueState:
        """Create one issue, using the draft digest as the retry identity."""
        ...

    def add_issue_comment(
        self,
        repository: str,
        issue_number: int,
        *,
        body: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> GitHubIssueState:
        """Append one comment only while the supplied target revision is current."""
        ...


class GitHubDesignPublisher:
    """Apply one exact append-only design draft through a narrow GitHub client."""

    __slots__ = ("_client", "_repository")

    def __init__(self, client: GitHubIssueClient, repository: str) -> None:
        required = ("get_issue", "create_issue", "add_issue_comment")
        if any(not callable(getattr(client, name, None)) for name in required):
            raise TypeError("GitHub self-design issue client is invalid")
        if type(repository) is not str or _REPOSITORY.fullmatch(repository) is None:
            raise ValueError("GitHub self-design repository identity is invalid")
        self._client = client
        self._repository = repository

    def publish(self, request: DesignPublicationRequest) -> DesignPublicationReceipt:
        """Revalidate local and remote evidence before applying one admitted effect."""
        if not isinstance(request, DesignPublicationRequest):
            raise TypeError("GitHub self-design publication request is invalid")
        if request.draft.repository != self._repository:
            raise DesignPublicationError("self-design publication repository is not admitted")

        self._verify_local_record(request)
        draft = request.draft
        current: GitHubIssueState | None = None
        try:
            if draft.effect is DesignPublicationEffect.CREATE_ISSUE:
                assert draft.title is not None
                published = self._client.create_issue(
                    self._repository,
                    title=draft.title,
                    body=draft.body,
                    idempotency_key=draft.digest,
                )
            else:
                assert draft.target_issue_number is not None
                assert draft.expected_target_revision is not None
                current = self._client.get_issue(
                    self._repository,
                    draft.target_issue_number,
                )
                self._verify_issue_binding(current, draft.target_issue_number)
                if current.revision != draft.expected_target_revision:
                    raise DesignPublicationError("self-design publication target revision is stale")
                published = self._client.add_issue_comment(
                    self._repository,
                    draft.target_issue_number,
                    body=draft.body,
                    expected_revision=draft.expected_target_revision,
                    idempotency_key=draft.digest,
                )
        except DesignPublicationError:
            raise
        except Exception as error:
            raise DesignPublicationError("GitHub self-design publication failed") from error

        self._verify_published_result(draft, published, current)
        return DesignPublicationReceipt(
            status=DesignPublicationStatus.PUBLISHED,
            effect=draft.effect,
            repository=self._repository,
            issue_number=published.issue_number,
            target_revision=published.revision,
            draft_digest=draft.digest,
        )

    def _verify_published_result(
        self,
        draft: DesignPublicationDraft,
        published: GitHubIssueState,
        previous: GitHubIssueState | None,
    ) -> None:
        if not isinstance(published, GitHubIssueState):
            raise TypeError("GitHub self-design issue client returned invalid state")
        expected_number = draft.target_issue_number
        if published.repository != self._repository or (
            expected_number is not None and published.issue_number != expected_number
        ):
            raise DesignPublicationError("GitHub self-design publication result is misbound")

        if draft.effect is DesignPublicationEffect.CREATE_ISSUE:
            if (
                published.title != draft.title
                or published.body != draft.body
                or published.status is not GitHubIssueStatus.OPEN
                or published.comment_count != 0
            ):
                raise DesignPublicationError(
                    "GitHub self-design issue creation result is inconsistent"
                )
            return

        if previous is None:
            raise TypeError("GitHub self-design publication target state is missing")
        if (
            published.title != previous.title
            or published.body != previous.body
            or published.status is not previous.status
            or published.comment_count != previous.comment_count + 1
            or published.updated_at < previous.updated_at
        ):
            raise DesignPublicationError(
                "GitHub self-design comment result is not append-only"
            )

    def _verify_issue_binding(self, issue: GitHubIssueState, issue_number: int) -> None:
        if not isinstance(issue, GitHubIssueState):
            raise TypeError("GitHub self-design issue client returned invalid state")
        if issue.repository != self._repository or issue.issue_number != issue_number:
            raise DesignPublicationError("GitHub self-design target state is misbound")

    @staticmethod
    def _verify_local_record(request: DesignPublicationRequest) -> None:
        expected = request.recorded.record.canonical_bytes()
        path = request.recorded.record_path
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            before = os.lstat(path)
            if stat.S_ISLNK(before.st_mode):
                raise DesignPublicationError("self-design publication local record is invalid")
            descriptor = os.open(path, flags)
        except DesignPublicationError:
            raise
        except OSError as error:
            raise DesignPublicationError(
                "self-design publication local record is unavailable"
            ) from error

        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_size > SELF_DESIGN_RECORD_MAX_BYTES
            ):
                raise DesignPublicationError("self-design publication local record is invalid")
            observed = bytearray()
            while len(observed) <= SELF_DESIGN_RECORD_MAX_BYTES:
                remaining = SELF_DESIGN_RECORD_MAX_BYTES + 1 - len(observed)
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                observed.extend(chunk)
        except OSError as error:
            raise DesignPublicationError(
                "self-design publication local record is unreadable"
            ) from error
        finally:
            os.close(descriptor)

        if bytes(observed) != expected:
            raise DesignPublicationError("self-design publication local record has changed")
