"""Governed contracts for publishing one exact recorded self-design plan."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from af.framework.errors import AFError
from af.framework.self_design import DesignRoute, DesignUnitKind
from af.framework.self_design_operation import PreparedDesignPlan
from af.framework.self_design_recording import DesignRecordResult

_MAX_AUTHORITY_LIFETIME_SECONDS = 3_600
_MAX_BODY_BYTES = 128 * 1024
_MAX_TITLE_LENGTH = 256
_AUTHORITY_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_REF = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,254})$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRACE_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")


class DesignPublicationError(AFError):
    """A sanitized fail-closed self-design publication error."""


class DesignPublicationEffect(StrEnum):
    """Append-only external effects admitted by the first publication slice."""

    CREATE_ISSUE = "create_issue"
    ADD_ISSUE_COMMENT = "add_issue_comment"


class DesignPublicationStatus(StrEnum):
    """Stable outcomes returned by a publication adapter."""

    PUBLISHED = "published"
    UNCHANGED = "unchanged"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"self-design publication {field_name} must be timezone aware")
    return value.astimezone(UTC)


def _iso8601(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_trace_id(value: object) -> bool:
    return type(value) is str and _TRACE_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class DesignPublicationDraft:
    """One exact append-only issue publication derived from an admitted design plan."""

    effect: DesignPublicationEffect
    repository: str
    selected_candidate_id: str
    unit_kind: DesignUnitKind
    objective_id: str
    acceptance_clause_ids: tuple[str, ...]
    target_issue_number: int | None
    expected_target_revision: str | None
    title: str | None
    body: str

    def __post_init__(self) -> None:
        if not isinstance(self.effect, DesignPublicationEffect):
            raise ValueError("self-design publication effect is invalid")
        if type(self.repository) is not str or _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("self-design publication repository identity is invalid")
        if not _valid_trace_id(self.selected_candidate_id):
            raise ValueError("self-design publication candidate identity is invalid")
        if not isinstance(self.unit_kind, DesignUnitKind):
            raise ValueError("self-design publication unit kind is invalid")
        if not _valid_trace_id(self.objective_id):
            raise ValueError("self-design publication objective identity is invalid")
        if (
            type(self.acceptance_clause_ids) is not tuple
            or not self.acceptance_clause_ids
            or len(self.acceptance_clause_ids) > 256
            or any(not _valid_trace_id(item) for item in self.acceptance_clause_ids)
            or len(set(self.acceptance_clause_ids)) != len(self.acceptance_clause_ids)
        ):
            raise ValueError("self-design publication acceptance identities are invalid")
        object.__setattr__(
            self,
            "acceptance_clause_ids",
            tuple(sorted(self.acceptance_clause_ids)),
        )
        if type(self.body) is not str or not self.body.strip() or "\x00" in self.body:
            raise ValueError("self-design publication body is invalid")
        if len(self.body.encode("utf-8")) > _MAX_BODY_BYTES:
            raise ValueError("self-design publication body is too large")

        if self.effect is DesignPublicationEffect.CREATE_ISSUE:
            if self.unit_kind is not DesignUnitKind.CHILD_TASK:
                raise ValueError("self-design issue creation requires a child task")
            if self.target_issue_number is not None or self.expected_target_revision is not None:
                raise ValueError("self-design issue creation cannot target existing state")
            if (
                type(self.title) is not str
                or not self.title.strip()
                or len(self.title) > _MAX_TITLE_LENGTH
                or "\x00" in self.title
            ):
                raise ValueError("self-design publication title is invalid")
        else:
            if self.unit_kind is DesignUnitKind.CHILD_TASK:
                raise ValueError("self-design child tasks must create an issue")
            if type(self.target_issue_number) is not int or self.target_issue_number < 1:
                raise ValueError("self-design publication target issue is invalid")
            if (
                type(self.expected_target_revision) is not str
                or _SHA256.fullmatch(self.expected_target_revision) is None
            ):
                raise ValueError("self-design publication target revision is invalid")
            if self.title is not None:
                raise ValueError("self-design issue comments cannot replace a title")

    def canonical_json(self) -> str:
        """Return the byte-stable draft representation bound into authority."""
        return _canonical_json(
            {
                "acceptance_clause_ids": list(self.acceptance_clause_ids),
                "body": self.body,
                "effect": self.effect.value,
                "expected_target_revision": self.expected_target_revision,
                "objective_id": self.objective_id,
                "repository": self.repository,
                "selected_candidate_id": self.selected_candidate_id,
                "target_issue_number": self.target_issue_number,
                "title": self.title,
                "unit_kind": self.unit_kind.value,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class DesignPublicationAuthority:
    """Exact, expiring authority for one draft and one external append-only effect."""

    authority_id: str
    repository: str
    base_ref: str
    head_sha: str
    snapshot_digest: str
    plan_digest: str
    record_digest: str
    record_path: str
    draft_digest: str
    issued_at: datetime
    expires_at: datetime
    allowed_effects: tuple[DesignPublicationEffect, ...]

    def __post_init__(self) -> None:
        if type(self.authority_id) is not str or _AUTHORITY_ID.fullmatch(self.authority_id) is None:
            raise ValueError("self-design publication authority identity is invalid")
        if type(self.repository) is not str or _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("self-design publication repository identity is invalid")
        if type(self.base_ref) is not str or _REF.fullmatch(self.base_ref) is None:
            raise ValueError("self-design publication base ref is invalid")
        if type(self.head_sha) is not str or _SHA1.fullmatch(self.head_sha) is None:
            raise ValueError("self-design publication head SHA is invalid")
        for value, label in (
            (self.snapshot_digest, "snapshot digest"),
            (self.plan_digest, "plan digest"),
            (self.record_digest, "record digest"),
            (self.draft_digest, "draft digest"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"self-design publication {label} is invalid")
        if type(self.record_path) is not str or not Path(self.record_path).is_absolute():
            raise ValueError("self-design publication record path must be absolute")
        issued_at = _aware_utc(self.issued_at, "authority issue time")
        expires_at = _aware_utc(self.expires_at, "authority expiry time")
        if expires_at <= issued_at:
            raise ValueError("self-design publication authority expiry is invalid")
        if (expires_at - issued_at).total_seconds() > _MAX_AUTHORITY_LIFETIME_SECONDS:
            raise ValueError("self-design publication authority lifetime is too long")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        if (
            type(self.allowed_effects) is not tuple
            or len(self.allowed_effects) != 1
            or not isinstance(self.allowed_effects[0], DesignPublicationEffect)
        ):
            raise ValueError("self-design publication authority grants unsupported effects")

    def canonical_json(self) -> str:
        """Return the deterministic authority representation."""
        return _canonical_json(
            {
                "allowed_effects": [effect.value for effect in self.allowed_effects],
                "authority_id": self.authority_id,
                "base_ref": self.base_ref,
                "draft_digest": self.draft_digest,
                "expires_at": _iso8601(self.expires_at),
                "head_sha": self.head_sha,
                "issued_at": _iso8601(self.issued_at),
                "plan_digest": self.plan_digest,
                "record_digest": self.record_digest,
                "record_path": self.record_path,
                "repository": self.repository,
                "snapshot_digest": self.snapshot_digest,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class DesignPublicationRequest:
    """One recorded proposal, exact draft, and bounded publication authority."""

    prepared: PreparedDesignPlan
    recorded: DesignRecordResult
    draft: DesignPublicationDraft
    authority: DesignPublicationAuthority
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.prepared, PreparedDesignPlan):
            raise TypeError("self-design prepared plan is invalid")
        if not isinstance(self.recorded, DesignRecordResult):
            raise TypeError("self-design record result is invalid")
        if not isinstance(self.draft, DesignPublicationDraft):
            raise TypeError("self-design publication draft is invalid")
        if not isinstance(self.authority, DesignPublicationAuthority):
            raise TypeError("self-design publication authority is invalid")
        observed_at = _aware_utc(self.observed_at, "observation time")
        object.__setattr__(self, "observed_at", observed_at)
        if not self.authority.issued_at <= observed_at <= self.authority.expires_at:
            raise DesignPublicationError("self-design publication authority is not currently valid")

        plan = self.prepared.plan
        snapshot = self.prepared.snapshot
        record = self.recorded.record
        if plan.route is not DesignRoute.PROPOSE:
            raise DesignPublicationError("self-design stop plans cannot be published")
        trace_matches = (
            self.draft.repository == snapshot.repository
            and self.draft.selected_candidate_id == plan.selected_candidate_id
            and self.draft.unit_kind is plan.unit_kind
            and self.draft.objective_id == plan.objective_id
            and self.draft.acceptance_clause_ids == plan.acceptance_clause_ids
        )
        record_matches = (
            record.prepared.snapshot.digest == snapshot.digest
            and record.prepared.plan.digest == plan.digest
        )
        authority_matches = (
            self.authority.repository == snapshot.repository
            and self.authority.base_ref == snapshot.base_ref
            and self.authority.head_sha == snapshot.head_sha
            and self.authority.snapshot_digest == snapshot.digest
            and self.authority.plan_digest == plan.digest
            and self.authority.record_digest == record.digest
            and self.authority.record_path == self.recorded.record_path
            and self.authority.draft_digest == self.draft.digest
            and self.authority.allowed_effects == (self.draft.effect,)
        )
        if not trace_matches:
            raise DesignPublicationError("self-design publication traceability is invalid")
        if not record_matches:
            raise DesignPublicationError("self-design publication local record is invalid")
        if not authority_matches:
            raise DesignPublicationError("self-design publication evidence binding is invalid")


@dataclass(frozen=True, slots=True)
class DesignPublicationReceipt:
    """Typed result from one exact external publication attempt."""

    status: DesignPublicationStatus
    effect: DesignPublicationEffect
    repository: str
    issue_number: int
    target_revision: str
    draft_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, DesignPublicationStatus):
            raise TypeError("self-design publication status is invalid")
        if not isinstance(self.effect, DesignPublicationEffect):
            raise TypeError("self-design publication receipt effect is invalid")
        if type(self.repository) is not str or _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("self-design publication receipt repository is invalid")
        if type(self.issue_number) is not int or self.issue_number < 1:
            raise ValueError("self-design publication receipt issue is invalid")
        for value, label in (
            (self.target_revision, "target revision"),
            (self.draft_digest, "draft digest"),
        ):
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"self-design publication receipt {label} is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "draft_digest": self.draft_digest,
            "effect": self.effect.value,
            "issue_number": self.issue_number,
            "repository": self.repository,
            "status": self.status.value,
            "target_revision": self.target_revision,
        }


class DesignPublisher(Protocol):
    """Publish one exact request after re-verifying target and local-record state."""

    def publish(self, request: DesignPublicationRequest) -> DesignPublicationReceipt:
        """Apply the one admitted append-only effect or fail closed."""
        ...
