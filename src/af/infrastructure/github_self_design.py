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
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class GitHubIssueStatus(StrEnum):
    """Issue states used by the narrow publication adapter boundary."""

    OPEN = "open"
    CLOSED = "closed"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"GitHub {field_name} must be timezone aware")
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
    latest_comment_id: int | None = None

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
        object.__setattr__(
            self,
            "updated_at",
            _aware_utc(self.updated_at, "issue update time"),
        )
        if type(self.comment_count) is not int or self.comment_count < 0:
            raise ValueError("GitHub issue comment count is invalid")
        if self.latest_comment_id is not None and (
            type(self.latest_comment_id) is not int or self.latest_comment_id < 1
        ):
            raise ValueError("GitHub issue latest comment identity is invalid")
        if (self.comment_count == 0) is not (self.latest_comment_id is None):
            raise ValueError("GitHub issue comment watermark is inconsistent")

    def canonical_json(self) -> str:
        """Return the deterministic external target representation."""
        return _canonical_json(
            {
                "body": self.body,
                "comment_count": self.comment_count,
                "issue_number": self.issue_number,
                "latest_comment_id": self.latest_comment_id,
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


@dataclass(frozen=True, slots=True)
class GitHubIssueCreation:
    """Canonical evidence binding one created issue to its exact draft request."""

    issue: GitHubIssueState
    draft_digest: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.issue, GitHubIssueState):
            raise TypeError("GitHub issue creation state is invalid")
        if type(self.draft_digest) is not str or _SHA256.fullmatch(self.draft_digest) is None:
            raise ValueError("GitHub issue creation draft digest is invalid")
        created_at = _aware_utc(self.created_at, "issue creation time")
        object.__setattr__(self, "created_at", created_at)
        if created_at > self.issue.updated_at:
            raise ValueError("GitHub issue creation transition is invalid")

    def canonical_json(self) -> str:
        """Return deterministic, independently readable creation evidence."""
        return _canonical_json(
            {
                "created_at": self.created_at.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                ),
                "draft_digest": self.draft_digest,
                "issue_revision": self.issue.revision,
            }
        )

    @property
    def revision(self) -> str:
        """Return the exact digest for this creation evidence."""
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class GitHubIssueComment:
    """Canonical evidence for one exact append-only issue comment."""

    repository: str
    issue_number: int
    comment_id: int
    body: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if type(self.repository) is not str or _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("GitHub comment repository identity is invalid")
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("GitHub comment issue number is invalid")
        if type(self.comment_id) is not int or self.comment_id < 1:
            raise ValueError("GitHub comment identity is invalid")
        if (
            type(self.body) is not str
            or "\x00" in self.body
            or len(self.body.encode("utf-8")) > _MAX_ISSUE_BODY_BYTES
        ):
            raise ValueError("GitHub comment body is invalid")
        object.__setattr__(
            self,
            "updated_at",
            _aware_utc(self.updated_at, "comment update time"),
        )

    def canonical_json(self) -> str:
        """Return the deterministic appended-comment representation."""
        return _canonical_json(
            {
                "body": self.body,
                "comment_id": self.comment_id,
                "issue_number": self.issue_number,
                "repository": self.repository,
                "updated_at": self.updated_at.isoformat(timespec="microseconds").replace(
                    "+00:00", "Z"
                ),
            }
        )

    @property
    def revision(self) -> str:
        """Return the exact digest for this comment evidence."""
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class GitHubCommentPublication:
    """Immediate client evidence for one appended comment and resulting issue."""

    issue: GitHubIssueState
    comment: GitHubIssueComment

    def __post_init__(self) -> None:
        if not isinstance(self.issue, GitHubIssueState):
            raise TypeError("GitHub comment publication issue state is invalid")
        if not isinstance(self.comment, GitHubIssueComment):
            raise TypeError("GitHub comment publication evidence is invalid")
        if (
            self.comment.repository != self.issue.repository
            or self.comment.issue_number != self.issue.issue_number
        ):
            raise ValueError("GitHub comment publication evidence is misbound")


class GitHubIssueClient(Protocol):
    """Narrow GitHub issue client admitted beneath the publication adapter."""

    def get_issue(self, repository: str, issue_number: int) -> GitHubIssueState:
        """Return the current canonical issue state."""
        ...

    def get_issue_creation(
        self,
        repository: str,
        issue_number: int,
    ) -> GitHubIssueCreation:
        """Return canonical evidence for one exact issue creation."""
        ...

    def get_issue_comment(
        self,
        repository: str,
        issue_number: int,
        comment_id: int,
    ) -> GitHubIssueComment:
        """Return canonical evidence for one exact comment identity."""
        ...

    def create_issue(
        self,
        repository: str,
        *,
        title: str,
        body: str,
        idempotency_key: str,
    ) -> GitHubIssueCreation:
        """Create one issue and return evidence bound to the retry identity."""
        ...

    def add_issue_comment(
        self,
        repository: str,
        issue_number: int,
        *,
        body: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> GitHubCommentPublication:
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
        published_comment: GitHubIssueComment | None = None
        published_creation: GitHubIssueCreation | None = None
        try:
            if draft.effect is DesignPublicationEffect.CREATE_ISSUE:
                assert draft.title is not None
                if not callable(getattr(self._client, "get_issue_creation", None)):
                    raise DesignPublicationError(
                        "GitHub self-design creation readback is unavailable"
                    )
                published_creation = self._client.create_issue(
                    self._repository,
                    title=draft.title,
                    body=draft.body,
                    idempotency_key=draft.digest,
                )
                if not isinstance(published_creation, GitHubIssueCreation):
                    raise TypeError("GitHub self-design creation evidence is invalid")
                published = published_creation.issue
            else:
                assert draft.target_issue_number is not None
                assert draft.expected_target_revision is not None
                if not callable(getattr(self._client, "get_issue_comment", None)):
                    raise DesignPublicationError(
                        "GitHub self-design comment readback is unavailable"
                    )
                current = self._client.get_issue(
                    self._repository,
                    draft.target_issue_number,
                )
                self._verify_issue_binding(current, draft.target_issue_number)
                if current.revision != draft.expected_target_revision:
                    raise DesignPublicationError("self-design publication target revision is stale")
                publication = self._client.add_issue_comment(
                    self._repository,
                    draft.target_issue_number,
                    body=draft.body,
                    expected_revision=draft.expected_target_revision,
                    idempotency_key=draft.digest,
                )
                if not isinstance(publication, GitHubCommentPublication):
                    raise TypeError("GitHub self-design comment publication evidence is invalid")
                published = publication.issue
                published_comment = publication.comment

            self._verify_published_result(draft, published, current)
            if published_creation is not None:
                self._verify_published_creation(request, published_creation)
                observed_creation = self._client.get_issue_creation(
                    self._repository,
                    published.issue_number,
                )
                self._verify_observed_creation(published_creation, observed_creation)
            if published_comment is not None:
                if current is None:
                    raise TypeError("GitHub self-design publication target state is missing")
                self._verify_published_comment(
                    draft,
                    current,
                    published,
                    published_comment,
                )
                observed_comment = self._client.get_issue_comment(
                    self._repository,
                    published.issue_number,
                    published_comment.comment_id,
                )
                self._verify_observed_comment(published_comment, observed_comment)
            observed = self._client.get_issue(self._repository, published.issue_number)
            self._verify_observed_result(published, observed)
        except DesignPublicationError:
            raise
        except Exception as error:
            raise DesignPublicationError("GitHub self-design publication failed") from error

        return DesignPublicationReceipt(
            status=DesignPublicationStatus.PUBLISHED,
            effect=draft.effect,
            repository=self._repository,
            issue_number=observed.issue_number,
            target_revision=observed.revision,
            draft_digest=draft.digest,
        )

    def _verify_published_creation(
        self,
        request: DesignPublicationRequest,
        creation: GitHubIssueCreation,
    ) -> None:
        if not isinstance(creation, GitHubIssueCreation):
            raise TypeError("GitHub self-design creation evidence is invalid")
        if creation.draft_digest != request.draft.digest:
            raise DesignPublicationError("GitHub self-design creation evidence is inconsistent")
        if not request.authority.issued_at <= creation.created_at <= request.authority.expires_at:
            raise DesignPublicationError(
                "GitHub self-design creation evidence is outside authority"
            )

    def _verify_observed_creation(
        self,
        published: GitHubIssueCreation,
        observed: GitHubIssueCreation,
    ) -> None:
        if not isinstance(published, GitHubIssueCreation) or not isinstance(
            observed, GitHubIssueCreation
        ):
            raise TypeError("GitHub self-design creation client returned invalid evidence")
        self._verify_issue_binding(observed.issue, published.issue.issue_number)
        if observed.revision != published.revision:
            raise DesignPublicationError("GitHub self-design creation readback drifted")

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
            or published.latest_comment_id is None
            or (
                previous.latest_comment_id is not None
                and published.latest_comment_id <= previous.latest_comment_id
            )
            or published.updated_at < previous.updated_at
        ):
            raise DesignPublicationError("GitHub self-design comment result is not append-only")

    def _verify_published_comment(
        self,
        draft: DesignPublicationDraft,
        previous: GitHubIssueState,
        published: GitHubIssueState,
        comment: GitHubIssueComment,
    ) -> None:
        if not isinstance(previous, GitHubIssueState):
            raise TypeError("GitHub self-design publication target state is missing")
        if not isinstance(comment, GitHubIssueComment):
            raise TypeError("GitHub self-design comment evidence is invalid")
        if (
            comment.repository != self._repository
            or comment.issue_number != published.issue_number
            or comment.comment_id != published.latest_comment_id
            or (
                previous.latest_comment_id is not None
                and comment.comment_id <= previous.latest_comment_id
            )
            or comment.body != draft.body
        ):
            raise DesignPublicationError("GitHub self-design comment result is inconsistent")
        if comment.updated_at < previous.updated_at or comment.updated_at > published.updated_at:
            raise DesignPublicationError("GitHub self-design comment transition is inconsistent")

    def _verify_observed_comment(
        self,
        published: GitHubIssueComment,
        observed: GitHubIssueComment,
    ) -> None:
        if not isinstance(published, GitHubIssueComment) or not isinstance(
            observed, GitHubIssueComment
        ):
            raise TypeError("GitHub self-design comment client returned invalid evidence")
        if (
            observed.repository != self._repository
            or observed.issue_number != published.issue_number
            or observed.comment_id != published.comment_id
        ):
            raise DesignPublicationError("GitHub self-design comment readback is misbound")
        if observed.revision != published.revision:
            raise DesignPublicationError("GitHub self-design comment readback drifted")

    def _verify_observed_result(
        self,
        published: GitHubIssueState,
        observed: GitHubIssueState,
    ) -> None:
        if not isinstance(published, GitHubIssueState) or not isinstance(
            observed, GitHubIssueState
        ):
            raise TypeError("GitHub self-design issue client returned invalid state")
        self._verify_issue_binding(observed, published.issue_number)
        if observed.revision != published.revision:
            raise DesignPublicationError("GitHub self-design post-publication state drifted")

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

            after_open = os.fstat(descriptor)
            after_path = os.lstat(path)
            identity = (opened.st_dev, opened.st_ino)
            stable_identity = (
                not stat.S_ISLNK(after_path.st_mode)
                and stat.S_ISREG(after_path.st_mode)
                and (before.st_dev, before.st_ino) == identity
                and (after_open.st_dev, after_open.st_ino) == identity
                and (after_path.st_dev, after_path.st_ino) == identity
            )
            stable_metadata = (
                before.st_size == opened.st_size == after_open.st_size == after_path.st_size
                and before.st_mtime_ns
                == opened.st_mtime_ns
                == after_open.st_mtime_ns
                == after_path.st_mtime_ns
                and before.st_ctime_ns
                == opened.st_ctime_ns
                == after_open.st_ctime_ns
                == after_path.st_ctime_ns
            )
            if not stable_identity or not stable_metadata:
                raise DesignPublicationError(
                    "self-design publication local record changed during verification"
                )
        except DesignPublicationError:
            raise
        except OSError as error:
            raise DesignPublicationError(
                "self-design publication local record is unreadable"
            ) from error
        finally:
            os.close(descriptor)

        if bytes(observed) != expected:
            raise DesignPublicationError("self-design publication local record has changed")
