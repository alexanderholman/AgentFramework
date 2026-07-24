"""Pure planning contracts for AF's governed self-design operation."""

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
_CANDIDATE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,95})$")
_TRACE_ID = re.compile(r"^[a-z][a-z0-9._:-]{0,127}$")
_MAX_CANDIDATES = 1_000
_MAX_METRIC = 10_000
_MAX_SNAPSHOT_AGE_SECONDS = 86_400


class DesignUnitKind(StrEnum):
    """Bounded design units understood by the first self-design slice."""

    EPIC_UPDATE = "epic_update"
    SPECIFICATION_UPDATE = "specification_update"
    CHILD_TASK = "child_task"
    ACCEPTANCE_REPAIR = "acceptance_repair"


class DesignReason(StrEnum):
    """Stable reasons for selecting or refusing a design candidate."""

    SELECTED = "selected"
    ELIGIBLE = "eligible"
    STALE_SNAPSHOT = "stale_snapshot"
    ACTIVE_OVERLAP = "active_overlap"
    MISSING_ACCEPTANCE = "missing_acceptance"
    DEPENDENCY_CYCLE = "dependency_cycle"
    APPEND_ONLY_CONFLICT = "append_only_conflict"
    MISSING_AUTHORITY = "missing_authority"
    MISSING_EVIDENCE = "missing_evidence"
    OBJECTIVE_UNTRACED = "objective_untraced"
    COMPLETE = "complete"
    NO_ELIGIBLE_DESIGN = "no_eligible_design"
    AMBIGUOUS_SELECTION = "ambiguous_selection"


class DesignRoute(StrEnum):
    """The only routes emitted by the read-only design planner."""

    STOP = "stop"
    PROPOSE = "propose"


def _aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"self-design {field_name} must be timezone aware")
    return value.astimezone(UTC)


def _iso8601(value: datetime) -> str:
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bounded_int(value: object, field_name: str, *, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"self-design {field_name} is invalid")
    return value


def _valid_trace_id(value: object) -> bool:
    return type(value) is str and _TRACE_ID.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class DesignCandidate:
    """One immutable proposed design unit and its fail-closed eligibility facts."""

    candidate_id: str
    revision: str
    kind: DesignUnitKind
    priority: int
    blocking_impact: int
    risk: int
    created_at: datetime
    objective_id: str | None
    acceptance_clause_ids: tuple[str, ...]
    dependency_ids: tuple[str, ...]
    active_overlap: bool
    acceptance_complete: bool
    append_only_safe: bool
    authority_granted: bool
    evidence_complete: bool
    complete: bool

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not str or _CANDIDATE_ID.fullmatch(self.candidate_id) is None:
            raise ValueError("self-design candidate identity is invalid")
        if type(self.revision) is not str or _SHA256.fullmatch(self.revision) is None:
            raise ValueError("self-design candidate revision is invalid")
        if not isinstance(self.kind, DesignUnitKind):
            raise ValueError("self-design unit kind is invalid")
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
        if self.objective_id is not None and not _valid_trace_id(self.objective_id):
            raise ValueError("self-design objective identity is invalid")
        if (
            type(self.acceptance_clause_ids) is not tuple
            or len(self.acceptance_clause_ids) > 256
            or any(not _valid_trace_id(item) for item in self.acceptance_clause_ids)
            or len(set(self.acceptance_clause_ids)) != len(self.acceptance_clause_ids)
        ):
            raise ValueError("self-design acceptance clause identities are invalid")
        if (
            type(self.dependency_ids) is not tuple
            or len(self.dependency_ids) > 256
            or any(
                type(item) is not str or _CANDIDATE_ID.fullmatch(item) is None
                for item in self.dependency_ids
            )
            or len(set(self.dependency_ids)) != len(self.dependency_ids)
            or self.candidate_id in self.dependency_ids
        ):
            raise ValueError("self-design dependency identities are invalid")
        object.__setattr__(
            self,
            "acceptance_clause_ids",
            tuple(sorted(self.acceptance_clause_ids)),
        )
        object.__setattr__(self, "dependency_ids", tuple(sorted(self.dependency_ids)))
        boolean_fields = (
            self.active_overlap,
            self.acceptance_complete,
            self.append_only_safe,
            self.authority_granted,
            self.evidence_complete,
            self.complete,
        )
        if any(type(value) is not bool for value in boolean_fields):
            raise ValueError("self-design eligibility facts must be boolean")

    def _payload(self) -> dict[str, object]:
        return {
            "acceptance_clause_ids": list(self.acceptance_clause_ids),
            "acceptance_complete": self.acceptance_complete,
            "active_overlap": self.active_overlap,
            "append_only_safe": self.append_only_safe,
            "authority_granted": self.authority_granted,
            "blocking_impact": self.blocking_impact,
            "candidate_id": self.candidate_id,
            "complete": self.complete,
            "created_at": _iso8601(self.created_at),
            "dependency_ids": list(self.dependency_ids),
            "evidence_complete": self.evidence_complete,
            "kind": self.kind.value,
            "objective_id": self.objective_id,
            "priority": self.priority,
            "revision": self.revision,
            "risk": self.risk,
        }


@dataclass(frozen=True, slots=True)
class DesignSnapshot:
    """One exact, bounded design observation used for a single plan."""

    source_id: str
    repository: str
    base_ref: str
    head_sha: str
    captured_at: datetime
    candidates: tuple[DesignCandidate, ...]

    def __post_init__(self) -> None:
        if type(self.source_id) is not str or _SOURCE_ID.fullmatch(self.source_id) is None:
            raise ValueError("self-design source identity is invalid")
        if type(self.repository) is not str or _REPOSITORY.fullmatch(self.repository) is None:
            raise ValueError("self-design repository identity is invalid")
        if type(self.base_ref) is not str or _REF.fullmatch(self.base_ref) is None:
            raise ValueError("self-design base ref is invalid")
        if type(self.head_sha) is not str or _SHA1.fullmatch(self.head_sha) is None:
            raise ValueError("self-design head SHA is invalid")
        object.__setattr__(
            self,
            "captured_at",
            _aware_utc(self.captured_at, "snapshot time"),
        )
        if (
            type(self.candidates) is not tuple
            or len(self.candidates) > _MAX_CANDIDATES
            or any(not isinstance(item, DesignCandidate) for item in self.candidates)
        ):
            raise ValueError("self-design candidates are invalid")
        ordered = tuple(sorted(self.candidates, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in ordered}) != len(ordered):
            raise ValueError("self-design candidate identities must be unique")
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
class DesignPolicy:
    """Explicit policy inputs that may influence deterministic design selection."""

    max_snapshot_age_seconds: int
    tie_break_by_id: bool

    def __post_init__(self) -> None:
        if (
            type(self.max_snapshot_age_seconds) is not int
            or not 1 <= self.max_snapshot_age_seconds <= _MAX_SNAPSHOT_AGE_SECONDS
        ):
            raise ValueError("self-design snapshot age policy is invalid")
        if type(self.tie_break_by_id) is not bool:
            raise ValueError("self-design tie-break policy is invalid")

    def canonical_json(self) -> str:
        """Return the byte-stable policy representation bound into decisions."""
        return _canonical_json(
            {
                "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
                "tie_break_by_id": self.tie_break_by_id,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class DesignAssessment:
    """The eligibility result for one observed design candidate."""

    candidate_id: str
    reason: DesignReason

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not str or _CANDIDATE_ID.fullmatch(self.candidate_id) is None:
            raise ValueError("self-design assessment identity is invalid")
        if not isinstance(self.reason, DesignReason):
            raise ValueError("self-design assessment reason is invalid")

    def _payload(self) -> dict[str, str]:
        return {"candidate_id": self.candidate_id, "reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class DesignDecision:
    """A single-candidate decision bound to one immutable design snapshot."""

    snapshot_digest: str
    policy_digest: str
    selected_candidate_id: str | None
    reason: DesignReason
    assessments: tuple[DesignAssessment, ...]

    def __post_init__(self) -> None:
        if type(self.snapshot_digest) is not str or _SHA256.fullmatch(self.snapshot_digest) is None:
            raise ValueError("self-design decision snapshot digest is invalid")
        if type(self.policy_digest) is not str or _SHA256.fullmatch(self.policy_digest) is None:
            raise ValueError("self-design decision policy digest is invalid")
        if self.selected_candidate_id is not None and (
            type(self.selected_candidate_id) is not str
            or _CANDIDATE_ID.fullmatch(self.selected_candidate_id) is None
        ):
            raise ValueError("self-design selected identity is invalid")
        if not isinstance(self.reason, DesignReason):
            raise ValueError("self-design decision reason is invalid")
        if type(self.assessments) is not tuple or any(
            not isinstance(item, DesignAssessment) for item in self.assessments
        ):
            raise ValueError("self-design assessments are invalid")
        ordered = tuple(sorted(self.assessments, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in ordered}) != len(ordered):
            raise ValueError("self-design assessment identities must be unique")
        object.__setattr__(self, "assessments", ordered)
        if (self.reason is DesignReason.SELECTED) != (self.selected_candidate_id is not None):
            raise ValueError("self-design selected decision is inconsistent")

    def _payload(self) -> dict[str, object]:
        return {
            "assessments": [item._payload() for item in self.assessments],
            "policy_digest": self.policy_digest,
            "reason": self.reason.value,
            "selected_candidate_id": self.selected_candidate_id,
            "snapshot_digest": self.snapshot_digest,
        }


@dataclass(frozen=True, slots=True)
class DesignPlan:
    """A canonical read-only plan; this type intentionally grants no effects."""

    decision: DesignDecision
    route: DesignRoute
    selected_candidate_id: str | None
    unit_kind: DesignUnitKind | None
    objective_id: str | None
    acceptance_clause_ids: tuple[str, ...]
    allowed_writes: tuple[str, ...]
    stop_conditions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.decision, DesignDecision):
            raise ValueError("self-design plan decision is invalid")
        if not isinstance(self.route, DesignRoute):
            raise ValueError("self-design plan route is invalid")
        if self.selected_candidate_id != self.decision.selected_candidate_id:
            raise ValueError("self-design plan selection is inconsistent")
        if self.route is DesignRoute.STOP and (
            self.selected_candidate_id is not None
            or self.unit_kind is not None
            or self.objective_id is not None
            or self.acceptance_clause_ids
        ):
            raise ValueError("self-design stop plan cannot propose a design unit")
        if self.route is DesignRoute.PROPOSE and (
            self.selected_candidate_id is None
            or not isinstance(self.unit_kind, DesignUnitKind)
            or not _valid_trace_id(self.objective_id)
            or not self.acceptance_clause_ids
        ):
            raise ValueError("self-design proposal must carry complete traceability")
        if (
            type(self.acceptance_clause_ids) is not tuple
            or any(not _valid_trace_id(item) for item in self.acceptance_clause_ids)
            or len(set(self.acceptance_clause_ids)) != len(self.acceptance_clause_ids)
        ):
            raise ValueError("self-design plan acceptance identities are invalid")
        object.__setattr__(
            self,
            "acceptance_clause_ids",
            tuple(sorted(self.acceptance_clause_ids)),
        )
        if type(self.allowed_writes) is not tuple or self.allowed_writes:
            raise ValueError("self-design planning slice cannot grant writes")
        if (
            type(self.stop_conditions) is not tuple
            or not self.stop_conditions
            or any(not _valid_trace_id(item) for item in self.stop_conditions)
            or len(set(self.stop_conditions)) != len(self.stop_conditions)
        ):
            raise ValueError("self-design stop conditions are invalid")
        object.__setattr__(self, "stop_conditions", tuple(sorted(self.stop_conditions)))

    @property
    def snapshot_digest(self) -> str:
        return self.decision.snapshot_digest

    def canonical_json(self) -> str:
        """Return a deterministic serialized plan suitable for replay."""
        return _canonical_json(
            {
                "acceptance_clause_ids": list(self.acceptance_clause_ids),
                "allowed_writes": list(self.allowed_writes),
                "decision": self.decision._payload(),
                "objective_id": self.objective_id,
                "route": self.route.value,
                "selected_candidate_id": self.selected_candidate_id,
                "stop_conditions": list(self.stop_conditions),
                "unit_kind": self.unit_kind.value if self.unit_kind is not None else None,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


class SelfDesignPlanner:
    """Select at most one eligible design unit and emit a read-only plan."""

    _STOP_CONDITIONS = (
        "ambiguity",
        "authority_missing",
        "cancelled",
        "dependency_cycle",
        "evidence_missing",
        "overlap_detected",
        "snapshot_stale",
        "unsafe",
    )

    def plan(
        self,
        snapshot: DesignSnapshot,
        policy: DesignPolicy,
        *,
        now: datetime,
    ) -> DesignPlan:
        if not isinstance(snapshot, DesignSnapshot):
            raise TypeError("snapshot must be a DesignSnapshot")
        if not isinstance(policy, DesignPolicy):
            raise TypeError("policy must be a DesignPolicy")
        observed_now = _aware_utc(now, "planning time")
        if observed_now < snapshot.captured_at:
            raise ValueError("self-design planning time precedes snapshot")

        decision = self._select(snapshot, policy, now=observed_now)
        selected = (
            next(
                (
                    item
                    for item in snapshot.candidates
                    if item.candidate_id == decision.selected_candidate_id
                ),
                None,
            )
            if decision.selected_candidate_id is not None
            else None
        )
        if selected is None:
            return DesignPlan(
                decision=decision,
                route=DesignRoute.STOP,
                selected_candidate_id=None,
                unit_kind=None,
                objective_id=None,
                acceptance_clause_ids=(),
                allowed_writes=(),
                stop_conditions=self._STOP_CONDITIONS,
            )
        return DesignPlan(
            decision=decision,
            route=DesignRoute.PROPOSE,
            selected_candidate_id=selected.candidate_id,
            unit_kind=selected.kind,
            objective_id=selected.objective_id,
            acceptance_clause_ids=selected.acceptance_clause_ids,
            allowed_writes=(),
            stop_conditions=self._STOP_CONDITIONS,
        )

    def _select(
        self,
        snapshot: DesignSnapshot,
        policy: DesignPolicy,
        *,
        now: datetime,
    ) -> DesignDecision:
        age_seconds = (now - snapshot.captured_at).total_seconds()
        if age_seconds > policy.max_snapshot_age_seconds:
            return DesignDecision(
                snapshot_digest=snapshot.digest,
                policy_digest=policy.digest,
                selected_candidate_id=None,
                reason=DesignReason.STALE_SNAPSHOT,
                assessments=tuple(
                    DesignAssessment(item.candidate_id, DesignReason.STALE_SNAPSHOT)
                    for item in snapshot.candidates
                ),
            )

        cycle_ids = self._dependency_cycle_ids(snapshot.candidates)
        assessments = tuple(
            DesignAssessment(
                item.candidate_id,
                self._eligibility_reason(item, cycle_ids=cycle_ids),
            )
            for item in snapshot.candidates
        )
        reasons = {item.candidate_id: item.reason for item in assessments}
        eligible = tuple(
            item
            for item in snapshot.candidates
            if reasons[item.candidate_id] is DesignReason.ELIGIBLE
        )
        if not eligible:
            return DesignDecision(
                snapshot_digest=snapshot.digest,
                policy_digest=policy.digest,
                selected_candidate_id=None,
                reason=DesignReason.NO_ELIGIBLE_DESIGN,
                assessments=assessments,
            )

        best_key = min(self._order_key(item) for item in eligible)
        tied = tuple(item for item in eligible if self._order_key(item) == best_key)
        if len(tied) > 1 and not policy.tie_break_by_id:
            return DesignDecision(
                snapshot_digest=snapshot.digest,
                policy_digest=policy.digest,
                selected_candidate_id=None,
                reason=DesignReason.AMBIGUOUS_SELECTION,
                assessments=assessments,
            )
        selected = min(tied, key=lambda item: item.candidate_id)
        return DesignDecision(
            snapshot_digest=snapshot.digest,
            policy_digest=policy.digest,
            selected_candidate_id=selected.candidate_id,
            reason=DesignReason.SELECTED,
            assessments=assessments,
        )

    @staticmethod
    def _eligibility_reason(
        candidate: DesignCandidate,
        *,
        cycle_ids: frozenset[str],
    ) -> DesignReason:
        if candidate.complete:
            return DesignReason.COMPLETE
        if candidate.active_overlap:
            return DesignReason.ACTIVE_OVERLAP
        if not candidate.acceptance_complete or not candidate.acceptance_clause_ids:
            return DesignReason.MISSING_ACCEPTANCE
        if candidate.candidate_id in cycle_ids:
            return DesignReason.DEPENDENCY_CYCLE
        if not candidate.append_only_safe:
            return DesignReason.APPEND_ONLY_CONFLICT
        if not candidate.authority_granted:
            return DesignReason.MISSING_AUTHORITY
        if not candidate.evidence_complete:
            return DesignReason.MISSING_EVIDENCE
        if candidate.objective_id is None:
            return DesignReason.OBJECTIVE_UNTRACED
        return DesignReason.ELIGIBLE

    @staticmethod
    def _order_key(candidate: DesignCandidate) -> tuple[int, int, int, datetime]:
        return (
            candidate.priority,
            -candidate.blocking_impact,
            candidate.risk,
            candidate.created_at,
        )

    @staticmethod
    def _dependency_cycle_ids(
        candidates: tuple[DesignCandidate, ...],
    ) -> frozenset[str]:
        known = {item.candidate_id for item in candidates}
        graph = {
            item.candidate_id: tuple(
                dependency for dependency in item.dependency_ids if dependency in known
            )
            for item in candidates
        }
        state: dict[str, int] = {}
        cycle_ids: set[str] = set()

        for root_id in sorted(graph):
            if state.get(root_id, 0) != 0:
                continue
            state[root_id] = 1
            path = [root_id]
            path_index = {root_id: 0}
            stack = [(root_id, 0)]
            while stack:
                candidate_id, dependency_index = stack[-1]
                dependencies = graph[candidate_id]
                if dependency_index >= len(dependencies):
                    stack.pop()
                    path.pop()
                    path_index.pop(candidate_id)
                    state[candidate_id] = 2
                    continue
                dependency_id = dependencies[dependency_index]
                stack[-1] = (candidate_id, dependency_index + 1)
                dependency_state = state.get(dependency_id, 0)
                if dependency_state == 0:
                    state[dependency_id] = 1
                    path_index[dependency_id] = len(path)
                    path.append(dependency_id)
                    stack.append((dependency_id, 0))
                elif dependency_state == 1:
                    cycle_ids.update(path[path_index[dependency_id] :])
        return frozenset(cycle_ids)
