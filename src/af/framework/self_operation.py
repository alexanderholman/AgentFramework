"""Shared immutable stage protocol contracts for governed self-operation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

_SOURCE_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38})/"
    r"[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,99})$"
)
_REF = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._/-]{0,254})$")
_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TRACE_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_CANDIDATE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})$")
_ROUTE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_DIGESTS = 4_096
_MAX_RECORDS = 256
_MAX_BUDGET = 1_000_000


class SelfOperationStage(StrEnum):
    """The governed self-operation lifecycle stages."""

    ANALYSIS = "analysis"
    ALIGNMENT = "alignment"
    PRIORITISATION = "prioritisation"
    SPECIFICATION = "specification"
    DESIGN = "design"
    CONTAINMENT = "containment"
    DEVELOPMENT = "development"
    ASSURANCE = "assurance"
    QUALIFICATION = "qualification"
    OPTIMISATION = "optimisation"


class StageReason(StrEnum):
    """Stable stage result and stop reasons."""

    SELECTED = "selected"
    STALE_SNAPSHOT = "stale_snapshot"
    ACTIVE_OVERLAP = "active_overlap"
    MISSING_EVIDENCE = "missing_evidence"
    MISSING_AUTHORITY = "missing_authority"
    COMPLETE = "complete"
    NO_ELIGIBLE_INPUT = "no_eligible_input"
    AMBIGUOUS_SELECTION = "ambiguous_selection"
    POLICY_DENIED = "policy_denied"
    UNSAFE = "unsafe"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"self-operation {field_name} must be timezone aware")
    return value.astimezone(UTC)


def _iso8601(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_int(value: object, field_name: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"self-operation {field_name} is invalid")
    return value


def _is_trace_id(value: object) -> bool:
    return type(value) is str and _TRACE_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class AuthorityRecord:
    """One exact authority fact bound into a stage snapshot."""

    authority_id: str
    scope_id: str
    granted: bool

    def __post_init__(self) -> None:
        if not _is_trace_id(self.authority_id):
            raise ValueError("self-operation authority identity is invalid")
        if not _is_trace_id(self.scope_id):
            raise ValueError("self-operation authority scope is invalid")
        if type(self.granted) is not bool:
            raise ValueError("self-operation authority grant is invalid")

    def _payload(self) -> dict[str, object]:
        return {
            "authority_id": self.authority_id,
            "granted": self.granted,
            "scope_id": self.scope_id,
        }


@dataclass(frozen=True, slots=True)
class StageBudget:
    """One bounded budget fact carried through a stage snapshot."""

    budget_id: str
    unit: str
    limit: int
    used: int

    def __post_init__(self) -> None:
        if not _is_trace_id(self.budget_id):
            raise ValueError("self-operation budget identity is invalid")
        if type(self.unit) is not str or _ROUTE_ID.fullmatch(self.unit) is None:
            raise ValueError("self-operation budget unit is invalid")
        object.__setattr__(
            self,
            "limit",
            _bounded_int(self.limit, "budget limit", maximum=_MAX_BUDGET),
        )
        object.__setattr__(
            self,
            "used",
            _bounded_int(self.used, "budget usage", maximum=_MAX_BUDGET),
        )
        if self.used > self.limit:
            raise ValueError("self-operation budget usage exceeds limit")

    def _payload(self) -> dict[str, object]:
        return {
            "budget_id": self.budget_id,
            "limit": self.limit,
            "unit": self.unit,
            "used": self.used,
        }


@dataclass(frozen=True, slots=True)
class SelfOperationSnapshot:
    """One immutable repository-level self-operation observation."""

    source_id: str
    repository: str
    base_ref: str
    head_sha: str
    captured_at: datetime
    evidence_digests: tuple[str, ...]
    authorities: tuple[AuthorityRecord, ...]
    budgets: tuple[StageBudget, ...]

    def __post_init__(self) -> None:
        if type(self.source_id) is not str or _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("self-operation source identity is invalid")
        if type(self.repository) is not str or _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("self-operation repository identity is invalid")
        if type(self.base_ref) is not str or _REF.fullmatch(self.base_ref) is None:
            raise ValueError("self-operation base ref is invalid")
        if type(self.head_sha) is not str or _SHA1.fullmatch(self.head_sha) is None:
            raise ValueError("self-operation head SHA is invalid")
        object.__setattr__(self, "captured_at", _aware_utc(self.captured_at, "snapshot time"))
        if (
            type(self.evidence_digests) is not tuple
            or len(self.evidence_digests) > _MAX_DIGESTS
            or any(
                type(item) is not str or _SHA256.fullmatch(item) is None
                for item in self.evidence_digests
            )
            or len(set(self.evidence_digests)) != len(self.evidence_digests)
        ):
            raise ValueError("self-operation evidence digests are invalid")
        if (
            type(self.authorities) is not tuple
            or len(self.authorities) > _MAX_RECORDS
            or any(not isinstance(item, AuthorityRecord) for item in self.authorities)
        ):
            raise ValueError("self-operation authorities are invalid")
        if (
            type(self.budgets) is not tuple
            or len(self.budgets) > _MAX_RECORDS
            or any(not isinstance(item, StageBudget) for item in self.budgets)
        ):
            raise ValueError("self-operation budgets are invalid")
        authorities = tuple(sorted(self.authorities, key=lambda item: item.authority_id))
        if len({item.authority_id for item in authorities}) != len(authorities):
            raise ValueError("self-operation authority identities must be unique")
        budgets = tuple(sorted(self.budgets, key=lambda item: item.budget_id))
        if len({item.budget_id for item in budgets}) != len(budgets):
            raise ValueError("self-operation budget identities must be unique")
        object.__setattr__(self, "evidence_digests", tuple(sorted(self.evidence_digests)))
        object.__setattr__(self, "authorities", authorities)
        object.__setattr__(self, "budgets", budgets)

    def _payload(self) -> dict[str, object]:
        return {
            "authorities": [item._payload() for item in self.authorities],
            "base_ref": self.base_ref,
            "budgets": [item._payload() for item in self.budgets],
            "captured_at": _iso8601(self.captured_at),
            "evidence_digests": list(self.evidence_digests),
            "head_sha": self.head_sha,
            "repository": self.repository,
            "source_id": self.source_id,
        }

    def canonical_json(self) -> str:
        """Return the byte-stable snapshot representation."""
        return _canonical_json(self._payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class StageHandoff:
    """One immutable predecessor-bound stage entry contract."""

    stage: SelfOperationStage
    source_snapshot_digest: str
    input_artifact_digest: str
    policy_digest: str
    predecessor_result_digest: str | None
    entry_authority_id: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, SelfOperationStage):
            raise ValueError("self-operation handoff stage is invalid")
        for field_name in ("source_snapshot_digest", "input_artifact_digest", "policy_digest"):
            value = getattr(self, field_name)
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"self-operation {field_name} is invalid")
        if self.predecessor_result_digest is not None and (
            type(self.predecessor_result_digest) is not str
            or _SHA256.fullmatch(self.predecessor_result_digest) is None
        ):
            raise ValueError("self-operation predecessor result digest is invalid")
        if self.entry_authority_id is not None and not _is_trace_id(self.entry_authority_id):
            raise ValueError("self-operation entry authority identity is invalid")
        if (self.predecessor_result_digest is None) == (self.entry_authority_id is None):
            raise ValueError("self-operation handoff must declare exactly one entry path")

    def _payload(self) -> dict[str, object]:
        return {
            "entry_authority_id": self.entry_authority_id,
            "input_artifact_digest": self.input_artifact_digest,
            "policy_digest": self.policy_digest,
            "predecessor_result_digest": self.predecessor_result_digest,
            "source_snapshot_digest": self.source_snapshot_digest,
            "stage": self.stage.value,
        }

    def canonical_json(self) -> str:
        """Return the byte-stable handoff representation."""
        return _canonical_json(self._payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class StageResult:
    """The exact output or typed stop from one stage evaluation."""

    stage: SelfOperationStage
    handoff_digest: str
    reason: StageReason
    output_digest: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.stage, SelfOperationStage):
            raise ValueError("self-operation result stage is invalid")
        if type(self.handoff_digest) is not str or _SHA256.fullmatch(self.handoff_digest) is None:
            raise ValueError("self-operation result handoff digest is invalid")
        if not isinstance(self.reason, StageReason):
            raise ValueError("self-operation result reason is invalid")
        if self.output_digest is not None and (
            type(self.output_digest) is not str or _SHA256.fullmatch(self.output_digest) is None
        ):
            raise ValueError("self-operation output digest is invalid")
        if (self.reason is StageReason.SELECTED) != (self.output_digest is not None):
            raise ValueError("self-operation result output is inconsistent")

    def _payload(self) -> dict[str, object]:
        return {
            "handoff_digest": self.handoff_digest,
            "output_digest": self.output_digest,
            "reason": self.reason.value,
            "stage": self.stage.value,
        }

    def canonical_json(self) -> str:
        """Return the byte-stable result representation."""
        return _canonical_json(self._payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class StagePlan:
    """One canonical read-only stage plan bound to an exact handoff."""

    stage: SelfOperationStage
    handoff: StageHandoff
    result: StageResult
    route: str
    allowed_writes: tuple[str, ...]
    stop_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stage, SelfOperationStage):
            raise ValueError("self-operation plan stage is invalid")
        if not isinstance(self.handoff, StageHandoff):
            raise ValueError("self-operation plan handoff is invalid")
        if not isinstance(self.result, StageResult):
            raise ValueError("self-operation plan result is invalid")
        if self.handoff.stage is not self.stage or self.result.stage is not self.stage:
            raise ValueError("self-operation plan stage binding is inconsistent")
        if self.result.handoff_digest != self.handoff.digest:
            raise ValueError("self-operation plan handoff binding is inconsistent")
        if type(self.route) is not str or _ROUTE_ID.fullmatch(self.route) is None:
            raise ValueError("self-operation plan route is invalid")
        if type(self.allowed_writes) is not tuple or self.allowed_writes:
            raise ValueError("self-operation planning slice cannot grant writes")
        if (
            type(self.stop_conditions) is not tuple
            or not self.stop_conditions
            or any(not _is_trace_id(item) for item in self.stop_conditions)
            or len(set(self.stop_conditions)) != len(self.stop_conditions)
        ):
            raise ValueError("self-operation stop conditions are invalid")
        object.__setattr__(self, "stop_conditions", tuple(sorted(self.stop_conditions)))
        if (self.route == "stop") != (self.result.output_digest is None):
            raise ValueError("self-operation plan route is inconsistent")

    def _payload(self) -> dict[str, object]:
        return {
            "allowed_writes": list(self.allowed_writes),
            "handoff": self.handoff._payload(),
            "result": self.result._payload(),
            "route": self.route,
            "stage": self.stage.value,
            "stop_conditions": list(self.stop_conditions),
        }

    def canonical_json(self) -> str:
        """Return the byte-stable plan representation."""
        return _canonical_json(self._payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())
