"""Pure planning contracts for AF's governed self-development operation."""

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
_ITEM_ID = re.compile(
    r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})$"
)
_PROCEDURE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,63})$")
_GATE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_MAX_CANDIDATES = 1_000
_MAX_METRIC = 10_000
_MAX_SNAPSHOT_AGE_SECONDS = 86_400


class WorkKind(StrEnum):
    """Repository work types understood by the first planning rung."""

    IMPLEMENTATION = "implementation"
    DEFECT_REPAIR = "defect_repair"
    CI_REPAIR = "ci_repair"
    REVIEW_REPAIR = "review_repair"
    RELEASE_GATE = "release_gate"
    CLOSURE = "closure"


class SelectionReason(StrEnum):
    """Stable reasons for selecting or refusing repository work."""

    SELECTED = "selected"
    ELIGIBLE = "eligible"
    BLOCKED_DEPENDENCY = "blocked_dependency"
    ACTIVE_OVERLAP = "active_overlap"
    STALE_SNAPSHOT = "stale_snapshot"
    AMBIGUOUS_SCOPE = "ambiguous_scope"
    MISSING_AUTHORITY = "missing_authority"
    POLICY_DENIED = "policy_denied"
    FAILED_EVIDENCE = "failed_evidence"
    COMPLETE = "complete"
    NO_ELIGIBLE_WORK = "no_eligible_work"
    AMBIGUOUS_SELECTION = "ambiguous_selection"


class CycleRoute(StrEnum):
    """The only routes emitted by the read-only planning slice."""

    STOP = "stop"
    REUSE = "reuse"
    ENGINEER_GAP = "engineer_gap"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"self-development {field_name} must be timezone aware")
    return value.astimezone(UTC)


def _iso8601(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_int(value: object, field_name: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"self-development {field_name} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class WorkCandidate:
    """One immutable observed work item and its fail-closed eligibility facts."""

    item_id: str
    revision: str
    kind: WorkKind
    priority: int
    blocking_impact: int
    risk: int
    created_at: datetime
    procedure_id: str | None
    dependencies_ready: bool
    active_overlap: bool
    scope_clear: bool
    authority_granted: bool
    policy_allowed: bool
    evidence_passed: bool
    complete: bool

    def __post_init__(self) -> None:
        if type(self.item_id) is not str or _ITEM_ID.fullmatch(self.item_id) is None:
            raise ValueError("self-development candidate identity is invalid")
        if type(self.revision) is not str or _SHA256.fullmatch(self.revision) is None:
            raise ValueError("self-development candidate revision is invalid")
        if not isinstance(self.kind, WorkKind):
            raise ValueError("self-development work kind is invalid")
        object.__setattr__(
            self,
            "priority",
            _bounded_int(self.priority, "priority", maximum=9),
        )
        object.__setattr__(
            self,
            "blocking_impact",
            _bounded_int(
                self.blocking_impact,
                "blocking impact",
                maximum=_MAX_METRIC,
            ),
        )
        object.__setattr__(
            self,
            "risk",
            _bounded_int(self.risk, "risk", maximum=_MAX_METRIC),
        )
        object.__setattr__(
            self,
            "created_at",
            _aware_utc(self.created_at, "candidate creation time"),
        )
        if self.procedure_id is not None and (
            type(self.procedure_id) is not str
            or _PROCEDURE_ID.fullmatch(self.procedure_id) is None
        ):
            raise ValueError("self-development procedure identity is invalid")
        boolean_fields = (
            self.dependencies_ready,
            self.active_overlap,
            self.scope_clear,
            self.authority_granted,
            self.policy_allowed,
            self.evidence_passed,
            self.complete,
        )
        if any(type(value) is not bool for value in boolean_fields):
            raise ValueError("self-development eligibility facts must be boolean")

    def _payload(self) -> dict[str, object]:
        return {
            "active_overlap": self.active_overlap,
            "authority_granted": self.authority_granted,
            "blocking_impact": self.blocking_impact,
            "complete": self.complete,
            "created_at": _iso8601(self.created_at),
            "dependencies_ready": self.dependencies_ready,
            "evidence_passed": self.evidence_passed,
            "item_id": self.item_id,
            "kind": self.kind.value,
            "policy_allowed": self.policy_allowed,
            "priority": self.priority,
            "procedure_id": self.procedure_id,
            "revision": self.revision,
            "risk": self.risk,
            "scope_clear": self.scope_clear,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentSnapshot:
    """One exact, bounded repository observation used for a single plan."""

    source_id: str
    repository: str
    base_ref: str
    head_sha: str
    captured_at: datetime
    candidates: tuple[WorkCandidate, ...]

    def __post_init__(self) -> None:
        if type(self.source_id) is not str or _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("self-development source identity is invalid")
        if (
            type(self.repository) is not str
            or _REPOSITORY.fullmatch(self.repository) is None
        ):
            raise ValueError("self-development repository identity is invalid")
        if type(self.base_ref) is not str or _REF.fullmatch(self.base_ref) is None:
            raise ValueError("self-development base ref is invalid")
        if type(self.head_sha) is not str or _SHA1.fullmatch(self.head_sha) is None:
            raise ValueError("self-development head SHA is invalid")
        object.__setattr__(
            self,
            "captured_at",
            _aware_utc(self.captured_at, "snapshot time"),
        )
        if (
            type(self.candidates) is not tuple
            or len(self.candidates) > _MAX_CANDIDATES
            or any(not isinstance(item, WorkCandidate) for item in self.candidates)
        ):
            raise ValueError("self-development candidates are invalid")
        ordered = tuple(sorted(self.candidates, key=lambda item: item.item_id))
        if len({item.item_id for item in ordered}) != len(ordered):
            raise ValueError("self-development candidate identities must be unique")
        object.__setattr__(self, "candidates", ordered)

    def canonical_json(self) -> str:
        """Return the byte-stable representation used for replay and identity."""
        return _canonical_json(
            {
                "base_ref": self.base_ref,
                "candidates": [item._payload() for item in self.candidates],
                "captured_at": _iso8601(self.captured_at),
                "head_sha": self.head_sha,
                "repository": self.repository,
                "source_id": self.source_id,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Explicit policy inputs that may influence deterministic selection."""

    max_snapshot_age_seconds: int
    tie_break_by_id: bool
    required_gates: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            type(self.max_snapshot_age_seconds) is not int
            or not 1 <= self.max_snapshot_age_seconds <= _MAX_SNAPSHOT_AGE_SECONDS
        ):
            raise ValueError("self-development snapshot age policy is invalid")
        if type(self.tie_break_by_id) is not bool:
            raise ValueError("self-development tie-break policy is invalid")
        if (
            type(self.required_gates) is not tuple
            or not self.required_gates
            or len(self.required_gates) > 64
            or any(
                type(gate) is not str or _GATE_ID.fullmatch(gate) is None
                for gate in self.required_gates
            )
            or len(set(self.required_gates)) != len(self.required_gates)
        ):
            raise ValueError("self-development gate policy is invalid")
        object.__setattr__(self, "required_gates", tuple(sorted(self.required_gates)))

    def canonical_json(self) -> str:
        """Return the byte-stable policy representation bound into decisions."""
        return _canonical_json(
            {
                "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
                "required_gates": list(self.required_gates),
                "tie_break_by_id": self.tie_break_by_id,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    """The eligibility result for one observed candidate."""

    item_id: str
    reason: SelectionReason

    def __post_init__(self) -> None:
        if type(self.item_id) is not str or _ITEM_ID.fullmatch(self.item_id) is None:
            raise ValueError("self-development assessment identity is invalid")
        if not isinstance(self.reason, SelectionReason):
            raise ValueError("self-development assessment reason is invalid")

    def _payload(self) -> dict[str, str]:
        return {"item_id": self.item_id, "reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    """A single-candidate decision bound to one immutable snapshot."""

    snapshot_digest: str
    policy_digest: str
    selected_item_id: str | None
    reason: SelectionReason
    assessments: tuple[CandidateAssessment, ...]

    def __post_init__(self) -> None:
        if (
            type(self.snapshot_digest) is not str
            or _SHA256.fullmatch(self.snapshot_digest) is None
        ):
            raise ValueError("self-development selection snapshot digest is invalid")
        if (
            type(self.policy_digest) is not str
            or _SHA256.fullmatch(self.policy_digest) is None
        ):
            raise ValueError("self-development selection policy digest is invalid")
        if self.selected_item_id is not None and (
            type(self.selected_item_id) is not str
            or _ITEM_ID.fullmatch(self.selected_item_id) is None
        ):
            raise ValueError("self-development selected identity is invalid")
        if not isinstance(self.reason, SelectionReason):
            raise ValueError("self-development selection reason is invalid")
        if (
            type(self.assessments) is not tuple
            or any(not isinstance(item, CandidateAssessment) for item in self.assessments)
        ):
            raise ValueError("self-development assessments are invalid")
        ordered = tuple(sorted(self.assessments, key=lambda item: item.item_id))
        if len({item.item_id for item in ordered}) != len(ordered):
            raise ValueError("self-development assessment identities must be unique")
        object.__setattr__(self, "assessments", ordered)
        if (self.reason is SelectionReason.SELECTED) != (
            self.selected_item_id is not None
        ):
            raise ValueError("self-development selected decision is inconsistent")

    def _payload(self) -> dict[str, object]:
        return {
            "assessments": [item._payload() for item in self.assessments],
            "policy_digest": self.policy_digest,
            "reason": self.reason.value,
            "selected_item_id": self.selected_item_id,
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class CyclePlan:
    """A canonical read-only plan; this type intentionally grants no effects."""

    selection: SelectionDecision
    route: CycleRoute
    selected_item_id: str | None
    procedure_id: str | None
    required_gates: tuple[str, ...]
    allowed_writes: tuple[str, ...]
    stop_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.selection, SelectionDecision):
            raise ValueError("self-development plan selection is invalid")
        if not isinstance(self.route, CycleRoute):
            raise ValueError("self-development cycle route is invalid")
        if self.selected_item_id != self.selection.selected_item_id:
            raise ValueError("self-development plan selection is inconsistent")
        if self.route is CycleRoute.STOP and (
            self.selected_item_id is not None or self.procedure_id is not None
        ):
            raise ValueError("self-development stop plan cannot select work")
        if self.route is CycleRoute.REUSE and self.procedure_id is None:
            raise ValueError("self-development reuse plan requires a procedure")
        if self.route is CycleRoute.ENGINEER_GAP and self.procedure_id is not None:
            raise ValueError("self-development gap plan cannot name a procedure")
        if self.route is not CycleRoute.STOP and self.selected_item_id is None:
            raise ValueError("self-development active plan requires selected work")
        if self.allowed_writes:
            raise ValueError("self-development planning slice cannot grant writes")

    @property
    def snapshot_digest(self) -> str:
        return self.selection.snapshot_digest

    def canonical_json(self) -> str:
        """Return a deterministic serialized plan suitable for replay."""
        return _canonical_json(
            {
                "allowed_writes": list(self.allowed_writes),
                "procedure_id": self.procedure_id,
                "required_gates": list(self.required_gates),
                "route": self.route.value,
                "selected_item_id": self.selected_item_id,
                "selection": self.selection._payload(),
                "stop_conditions": list(self.stop_conditions),
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


class SelfDevelopmentPlanner:
    """Select at most one eligible item and emit a side-effect-free cycle plan."""

    _STOP_CONDITIONS = (
        "ambiguity",
        "authority_missing",
        "budget_exhausted",
        "cancelled",
        "evidence_failed",
        "snapshot_stale",
        "unsafe",
    )

    def plan(
        self,
        snapshot: DevelopmentSnapshot,
        policy: SelectionPolicy,
        *,
        now: datetime,
    ) -> CyclePlan:
        if not isinstance(snapshot, DevelopmentSnapshot):
            raise TypeError("snapshot must be a DevelopmentSnapshot")
        if not isinstance(policy, SelectionPolicy):
            raise TypeError("policy must be a SelectionPolicy")
        observed_now = _aware_utc(now, "planning time")
        if observed_now < snapshot.captured_at:
            raise ValueError("self-development planning time precedes snapshot")

        decision = self._select(snapshot, policy, now=observed_now)
        selected = (
            next(
                (
                    item
                    for item in snapshot.candidates
                    if item.item_id == decision.selected_item_id
                ),
                None,
            )
            if decision.selected_item_id is not None
            else None
        )
        if selected is None:
            route = CycleRoute.STOP
            procedure_id = None
        elif selected.procedure_id is not None:
            route = CycleRoute.REUSE
            procedure_id = selected.procedure_id
        else:
            route = CycleRoute.ENGINEER_GAP
            procedure_id = None

        return CyclePlan(
            selection=decision,
            route=route,
            selected_item_id=decision.selected_item_id,
            procedure_id=procedure_id,
            required_gates=policy.required_gates,
            allowed_writes=(),
            stop_conditions=self._STOP_CONDITIONS,
        )

    def _select(
        self,
        snapshot: DevelopmentSnapshot,
        policy: SelectionPolicy,
        *,
        now: datetime,
    ) -> SelectionDecision:
        age_seconds = (now - snapshot.captured_at).total_seconds()
        if age_seconds > policy.max_snapshot_age_seconds:
            return SelectionDecision(
                snapshot_digest=snapshot.digest,
                policy_digest=policy.digest,
                selected_item_id=None,
                reason=SelectionReason.STALE_SNAPSHOT,
                assessments=tuple(
                    CandidateAssessment(item.item_id, SelectionReason.STALE_SNAPSHOT)
                    for item in snapshot.candidates
                ),
            )

        assessments = tuple(
            CandidateAssessment(item.item_id, self._eligibility_reason(item))
            for item in snapshot.candidates
        )
        reasons = {item.item_id: item.reason for item in assessments}
        eligible = tuple(
            item
            for item in snapshot.candidates
            if reasons[item.item_id] is SelectionReason.ELIGIBLE
        )
        if not eligible:
            return SelectionDecision(
                snapshot_digest=snapshot.digest,
                policy_digest=policy.digest,
                selected_item_id=None,
                reason=SelectionReason.NO_ELIGIBLE_WORK,
                assessments=assessments,
            )

        best_key = min(self._order_key(item) for item in eligible)
        tied = tuple(item for item in eligible if self._order_key(item) == best_key)
        if len(tied) > 1 and not policy.tie_break_by_id:
            return SelectionDecision(
                snapshot_digest=snapshot.digest,
                policy_digest=policy.digest,
                selected_item_id=None,
                reason=SelectionReason.AMBIGUOUS_SELECTION,
                assessments=assessments,
            )
        selected = min(tied, key=lambda item: item.item_id)
        return SelectionDecision(
            snapshot_digest=snapshot.digest,
            policy_digest=policy.digest,
            selected_item_id=selected.item_id,
            reason=SelectionReason.SELECTED,
            assessments=assessments,
        )

    @staticmethod
    def _eligibility_reason(candidate: WorkCandidate) -> SelectionReason:
        if candidate.complete:
            return SelectionReason.COMPLETE
        if not candidate.dependencies_ready:
            return SelectionReason.BLOCKED_DEPENDENCY
        if candidate.active_overlap:
            return SelectionReason.ACTIVE_OVERLAP
        if not candidate.scope_clear:
            return SelectionReason.AMBIGUOUS_SCOPE
        if not candidate.authority_granted:
            return SelectionReason.MISSING_AUTHORITY
        if not candidate.policy_allowed:
            return SelectionReason.POLICY_DENIED
        if not candidate.evidence_passed:
            return SelectionReason.FAILED_EVIDENCE
        return SelectionReason.ELIGIBLE

    @staticmethod
    def _order_key(candidate: WorkCandidate) -> tuple[int, int, int, datetime]:
        return (
            candidate.priority,
            -candidate.blocking_impact,
            candidate.risk,
            candidate.created_at,
        )
