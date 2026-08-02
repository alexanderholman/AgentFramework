"""Pure planning contracts for AF's governed self-alignment stage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from af.framework.self_analysis import AnalysisPlan, AnalysisRoute
from af.framework.self_operation import (
    _CANDIDATE_ID,
    _SHA256,
    AuthorityRecord,
    SelfOperationSnapshot,
    SelfOperationStage,
    StageHandoff,
    StagePlan,
    StageReason,
    StageResult,
    _aware_utc,
    _bounded_int,
    _canonical_json,
    _digest,
    _is_trace_id,
)

_MAX_CANDIDATES = 1_000
_MAX_IDS = 256
_MAX_METRIC = 10_000
_MAX_SNAPSHOT_AGE_SECONDS = 86_400


class AlignmentRoute(StrEnum):
    """The only routes emitted by the read-only alignment stage."""

    STOP = "stop"
    ALIGN = "align"


def _canonical_ids(value: object, field_name: str) -> tuple[str, ...]:
    if (
        type(value) is not tuple
        or len(value) > _MAX_IDS
        or any(not _is_trace_id(item) for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError(f"self-alignment {field_name} are invalid")
    return tuple(sorted(value))


@dataclass(frozen=True, slots=True)
class AlignmentCandidate:
    """One immutable alignment candidate and its fail-closed eligibility facts."""

    candidate_id: str
    revision: str
    objective_id: str
    priority: int
    risk: int
    created_at: datetime
    operator_intent_digest: str
    architecture_digest: str
    commitment_ids: tuple[str, ...]
    constraint_ids: tuple[str, ...]
    unresolved_conflict_ids: tuple[str, ...]
    active_overlap: bool
    intent_preserved: bool
    policy_consistent: bool
    architecture_consistent: bool
    commitments_consistent: bool
    authority_granted: bool
    evidence_complete: bool
    complete: bool

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not str or _CANDIDATE_ID.fullmatch(self.candidate_id) is None:
            raise ValueError("self-alignment candidate identity is invalid")
        if type(self.revision) is not str or _SHA256.fullmatch(self.revision) is None:
            raise ValueError("self-alignment candidate revision is invalid")
        if not _is_trace_id(self.objective_id):
            raise ValueError("self-alignment objective identity is invalid")
        object.__setattr__(
            self,
            "priority",
            _bounded_int(self.priority, "priority", maximum=9),
        )
        object.__setattr__(self, "risk", _bounded_int(self.risk, "risk", maximum=_MAX_METRIC))
        object.__setattr__(
            self,
            "created_at",
            _aware_utc(self.created_at, "candidate creation time"),
        )
        for field_name in ("operator_intent_digest", "architecture_digest"):
            value = getattr(self, field_name)
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"self-alignment {field_name} is invalid")
        object.__setattr__(
            self, "commitment_ids", _canonical_ids(self.commitment_ids, "commitment identities")
        )
        object.__setattr__(
            self, "constraint_ids", _canonical_ids(self.constraint_ids, "constraint identities")
        )
        object.__setattr__(
            self,
            "unresolved_conflict_ids",
            _canonical_ids(self.unresolved_conflict_ids, "unresolved conflict identities"),
        )
        boolean_fields = (
            self.active_overlap,
            self.intent_preserved,
            self.policy_consistent,
            self.architecture_consistent,
            self.commitments_consistent,
            self.authority_granted,
            self.evidence_complete,
            self.complete,
        )
        if any(type(value) is not bool for value in boolean_fields):
            raise ValueError("self-alignment eligibility facts must be boolean")

    def _payload(self) -> dict[str, object]:
        return {
            "active_overlap": self.active_overlap,
            "architecture_consistent": self.architecture_consistent,
            "architecture_digest": self.architecture_digest,
            "authority_granted": self.authority_granted,
            "candidate_id": self.candidate_id,
            "commitment_ids": list(self.commitment_ids),
            "commitments_consistent": self.commitments_consistent,
            "complete": self.complete,
            "constraint_ids": list(self.constraint_ids),
            "created_at": self.created_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "evidence_complete": self.evidence_complete,
            "intent_preserved": self.intent_preserved,
            "objective_id": self.objective_id,
            "operator_intent_digest": self.operator_intent_digest,
            "policy_consistent": self.policy_consistent,
            "priority": self.priority,
            "revision": self.revision,
            "risk": self.risk,
            "unresolved_conflict_ids": list(self.unresolved_conflict_ids),
        }


@dataclass(frozen=True, slots=True)
class AlignmentSnapshot:
    """One exact alignment observation bound to one admitted predecessor plan."""

    operation: SelfOperationSnapshot
    predecessor_plan: AnalysisPlan
    candidates: tuple[AlignmentCandidate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operation, SelfOperationSnapshot):
            raise ValueError("self-alignment operation snapshot is invalid")
        if not isinstance(self.predecessor_plan, AnalysisPlan):
            raise ValueError("self-alignment predecessor plan is invalid")
        if (
            type(self.candidates) is not tuple
            or len(self.candidates) > _MAX_CANDIDATES
            or any(not isinstance(item, AlignmentCandidate) for item in self.candidates)
        ):
            raise ValueError("self-alignment candidates are invalid")
        ordered = tuple(sorted(self.candidates, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in ordered}) != len(ordered):
            raise ValueError("self-alignment candidate identities must be unique")
        object.__setattr__(self, "candidates", ordered)

    def canonical_json(self) -> str:
        """Return the byte-stable alignment snapshot representation."""
        return _canonical_json(
            {
                "candidates": [item._payload() for item in self.candidates],
                "operation": self.operation.payload(),
                "predecessor_plan": json.loads(self.predecessor_plan.canonical_json()),
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class AlignmentPolicy:
    """Explicit policy inputs that influence deterministic alignment selection."""

    max_snapshot_age_seconds: int
    tie_break_by_id: bool
    required_authority_id: str
    required_scope_id: str

    def __post_init__(self) -> None:
        if (
            type(self.max_snapshot_age_seconds) is not int
            or not 1 <= self.max_snapshot_age_seconds <= _MAX_SNAPSHOT_AGE_SECONDS
        ):
            raise ValueError("self-alignment snapshot age policy is invalid")
        if type(self.tie_break_by_id) is not bool:
            raise ValueError("self-alignment tie-break policy is invalid")
        if not _is_trace_id(self.required_authority_id):
            raise ValueError("self-alignment required authority is invalid")
        if not _is_trace_id(self.required_scope_id):
            raise ValueError("self-alignment required scope is invalid")

    def canonical_json(self) -> str:
        """Return the byte-stable policy representation bound into the plan."""
        return _canonical_json(
            {
                "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
                "required_authority_id": self.required_authority_id,
                "required_scope_id": self.required_scope_id,
                "tie_break_by_id": self.tie_break_by_id,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class AlignmentAssessment:
    """The eligibility result for one observed alignment candidate."""

    candidate_id: str
    reason: StageReason

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not str or _CANDIDATE_ID.fullmatch(self.candidate_id) is None:
            raise ValueError("self-alignment assessment identity is invalid")
        if not isinstance(self.reason, StageReason):
            raise ValueError("self-alignment assessment reason is invalid")

    def _payload(self) -> dict[str, str]:
        return {"candidate_id": self.candidate_id, "reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class AlignmentOutput:
    """The canonical read-only output emitted by a successful alignment stage."""

    objective_id: str
    operator_intent_digest: str
    architecture_digest: str
    commitment_ids: tuple[str, ...]
    constraint_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _is_trace_id(self.objective_id):
            raise ValueError("self-alignment output objective identity is invalid")
        for field_name in ("operator_intent_digest", "architecture_digest"):
            value = getattr(self, field_name)
            if type(value) is not str or _SHA256.fullmatch(value) is None:
                raise ValueError(f"self-alignment output {field_name} is invalid")
        object.__setattr__(
            self, "commitment_ids", _canonical_ids(self.commitment_ids, "commitment identities")
        )
        object.__setattr__(
            self, "constraint_ids", _canonical_ids(self.constraint_ids, "constraint identities")
        )

    def _payload(self) -> dict[str, object]:
        return {
            "architecture_digest": self.architecture_digest,
            "commitment_ids": list(self.commitment_ids),
            "constraint_ids": list(self.constraint_ids),
            "objective_id": self.objective_id,
            "operator_intent_digest": self.operator_intent_digest,
        }

    def canonical_json(self) -> str:
        """Return the byte-stable alignment output representation."""
        return _canonical_json(self._payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class AlignmentPlan:
    """A canonical read-only self-alignment plan bound to one exact stage plan."""

    stage_plan: StagePlan
    route: AlignmentRoute
    selected_candidate_id: str | None
    objective_id: str | None
    output: AlignmentOutput | None
    assessments: tuple[AlignmentAssessment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stage_plan, StagePlan):
            raise ValueError("self-alignment stage plan is invalid")
        if self.stage_plan.stage is not SelfOperationStage.ALIGNMENT:
            raise ValueError("self-alignment stage plan stage is invalid")
        if not isinstance(self.route, AlignmentRoute):
            raise ValueError("self-alignment route is invalid")
        if self.selected_candidate_id is not None and (
            type(self.selected_candidate_id) is not str
            or _CANDIDATE_ID.fullmatch(self.selected_candidate_id) is None
        ):
            raise ValueError("self-alignment selected identity is invalid")
        if self.objective_id is not None and not _is_trace_id(self.objective_id):
            raise ValueError("self-alignment objective identity is invalid")
        if self.output is not None and not isinstance(self.output, AlignmentOutput):
            raise ValueError("self-alignment output is invalid")
        if type(self.assessments) is not tuple or any(
            not isinstance(item, AlignmentAssessment) for item in self.assessments
        ):
            raise ValueError("self-alignment assessments are invalid")
        ordered = tuple(sorted(self.assessments, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in ordered}) != len(ordered):
            raise ValueError("self-alignment assessment identities must be unique")
        object.__setattr__(self, "assessments", ordered)
        if self.route is AlignmentRoute.STOP:
            if (
                self.selected_candidate_id is not None
                or self.objective_id is not None
                or self.output is not None
                or self.stage_plan.route != "stop"
            ):
                raise ValueError("self-alignment stop plan is inconsistent")
        else:
            if (
                self.selected_candidate_id is None
                or self.objective_id is None
                or self.output is None
                or self.objective_id != self.output.objective_id
                or self.stage_plan.route != "align"
                or self.stage_plan.result.output_digest != self.output.digest
            ):
                raise ValueError("self-alignment active plan is inconsistent")

    def canonical_json(self) -> str:
        """Return the deterministic serialized plan suitable for replay."""
        return _canonical_json(
            {
                "assessments": [item._payload() for item in self.assessments],
                "architecture_digest": self.output.architecture_digest
                if self.output is not None
                else None,
                "commitment_ids": list(self.output.commitment_ids)
                if self.output is not None
                else None,
                "constraint_ids": list(self.output.constraint_ids)
                if self.output is not None
                else None,
                "objective_id": self.objective_id,
                "operator_intent_digest": self.output.operator_intent_digest
                if self.output is not None
                else None,
                "route": self.route.value,
                "selected_candidate_id": self.selected_candidate_id,
                "stage_plan": self.stage_plan.payload(),
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


class SelfAlignmentPlanner:
    """Select at most one eligible alignment input and emit a read-only plan."""

    _STOP_CONDITIONS = (
        "authority_missing",
        "cancelled",
        "evidence_missing",
        "policy_denied",
        "snapshot_stale",
        "unsafe",
    )

    def plan(
        self,
        snapshot: AlignmentSnapshot,
        policy: AlignmentPolicy,
        *,
        now: datetime,
    ) -> AlignmentPlan:
        if not isinstance(snapshot, AlignmentSnapshot):
            raise TypeError("snapshot must be an AlignmentSnapshot")
        if not isinstance(policy, AlignmentPolicy):
            raise TypeError("policy must be an AlignmentPolicy")
        observed_now = _aware_utc(now, "planning time")
        if observed_now < snapshot.operation.captured_at:
            raise ValueError("self-alignment planning time precedes snapshot")

        if not self._predecessor_is_admitted(snapshot):
            assessments = tuple(
                AlignmentAssessment(item.candidate_id, StageReason.MISSING_EVIDENCE)
                for item in snapshot.candidates
            )
            return self._stop_plan(
                snapshot,
                policy,
                assessments=assessments,
                reason=StageReason.MISSING_EVIDENCE,
            )

        required_authority = self._required_authority(
            snapshot.operation,
            policy.required_authority_id,
            policy.required_scope_id,
        )
        if required_authority is None or not required_authority.granted:
            assessments = tuple(
                AlignmentAssessment(item.candidate_id, StageReason.MISSING_AUTHORITY)
                for item in snapshot.candidates
            )
            return self._stop_plan(
                snapshot,
                policy,
                assessments=assessments,
                reason=StageReason.MISSING_AUTHORITY,
            )

        age_seconds = (observed_now - snapshot.operation.captured_at).total_seconds()
        if age_seconds > policy.max_snapshot_age_seconds:
            assessments = tuple(
                AlignmentAssessment(item.candidate_id, StageReason.STALE_SNAPSHOT)
                for item in snapshot.candidates
            )
            return self._stop_plan(
                snapshot,
                policy,
                assessments=assessments,
                reason=StageReason.STALE_SNAPSHOT,
            )

        assessments = tuple(
            AlignmentAssessment(item.candidate_id, self._eligibility_reason(item))
            for item in snapshot.candidates
        )
        reasons = {item.candidate_id: item.reason for item in assessments}
        eligible = tuple(
            item
            for item in snapshot.candidates
            if reasons[item.candidate_id] is StageReason.ELIGIBLE
        )
        if not eligible:
            return self._stop_plan(
                snapshot,
                policy,
                assessments=assessments,
                reason=StageReason.NO_ELIGIBLE_INPUT,
            )

        best_key = min(self._order_key(item) for item in eligible)
        tied = tuple(item for item in eligible if self._order_key(item) == best_key)
        if len(tied) > 1 and not policy.tie_break_by_id:
            return self._stop_plan(
                snapshot,
                policy,
                assessments=assessments,
                reason=StageReason.AMBIGUOUS_SELECTION,
            )
        selected = min(tied, key=lambda item: item.candidate_id)
        handoff = StageHandoff(
            stage=SelfOperationStage.ALIGNMENT,
            source_snapshot_digest=snapshot.operation.digest,
            input_artifact_digest=snapshot.digest,
            policy_digest=policy.digest,
            predecessor_result_digest=snapshot.predecessor_plan.stage_plan.result.digest,
            entry_authority_id=None,
        )
        output = AlignmentOutput(
            objective_id=selected.objective_id,
            operator_intent_digest=selected.operator_intent_digest,
            architecture_digest=selected.architecture_digest,
            commitment_ids=selected.commitment_ids,
            constraint_ids=selected.constraint_ids,
        )
        stage_plan = StagePlan(
            stage=SelfOperationStage.ALIGNMENT,
            handoff=handoff,
            result=StageResult(
                stage=SelfOperationStage.ALIGNMENT,
                handoff_digest=handoff.digest,
                reason=StageReason.SELECTED,
                output_digest=output.digest,
            ),
            route=AlignmentRoute.ALIGN.value,
            allowed_writes=(),
            stop_conditions=self._STOP_CONDITIONS,
        )
        selected_assessments = tuple(
            AlignmentAssessment(
                item.candidate_id,
                StageReason.SELECTED if item.candidate_id == selected.candidate_id else item.reason,
            )
            for item in assessments
        )
        return AlignmentPlan(
            stage_plan=stage_plan,
            route=AlignmentRoute.ALIGN,
            selected_candidate_id=selected.candidate_id,
            objective_id=selected.objective_id,
            output=output,
            assessments=selected_assessments,
        )

    @staticmethod
    def _required_authority(
        snapshot: SelfOperationSnapshot,
        authority_id: str,
        scope_id: str,
    ) -> AuthorityRecord | None:
        return next(
            (
                item
                for item in snapshot.authorities
                if item.authority_id == authority_id and item.scope_id == scope_id
            ),
            None,
        )

    @staticmethod
    def _predecessor_is_admitted(snapshot: AlignmentSnapshot) -> bool:
        predecessor_plan = snapshot.predecessor_plan
        predecessor_result = predecessor_plan.stage_plan.result
        return (
            predecessor_plan.stage_plan.stage is SelfOperationStage.ANALYSIS
            and predecessor_plan.route is AnalysisRoute.ANALYSE
            and predecessor_result.reason is StageReason.SELECTED
            and predecessor_result.output_digest is not None
            and predecessor_plan.stage_plan.handoff.source_snapshot_digest
            == snapshot.operation.digest
        )

    @staticmethod
    def _eligibility_reason(candidate: AlignmentCandidate) -> StageReason:
        if candidate.complete:
            return StageReason.COMPLETE
        if candidate.active_overlap:
            return StageReason.ACTIVE_OVERLAP
        if candidate.unresolved_conflict_ids:
            return StageReason.UNSAFE
        if (
            not candidate.intent_preserved
            or not candidate.policy_consistent
            or not candidate.architecture_consistent
            or not candidate.commitments_consistent
        ):
            return StageReason.POLICY_DENIED
        if not candidate.authority_granted:
            return StageReason.MISSING_AUTHORITY
        if not candidate.evidence_complete:
            return StageReason.MISSING_EVIDENCE
        return StageReason.ELIGIBLE

    @staticmethod
    def _order_key(candidate: AlignmentCandidate) -> tuple[int, int, datetime]:
        return (candidate.priority, candidate.risk, candidate.created_at)

    def _stop_plan(
        self,
        snapshot: AlignmentSnapshot,
        policy: AlignmentPolicy,
        *,
        assessments: tuple[AlignmentAssessment, ...],
        reason: StageReason,
    ) -> AlignmentPlan:
        handoff = StageHandoff(
            stage=SelfOperationStage.ALIGNMENT,
            source_snapshot_digest=snapshot.operation.digest,
            input_artifact_digest=snapshot.digest,
            policy_digest=policy.digest,
            predecessor_result_digest=snapshot.predecessor_plan.stage_plan.result.digest,
            entry_authority_id=None,
        )
        return AlignmentPlan(
            stage_plan=StagePlan(
                stage=SelfOperationStage.ALIGNMENT,
                handoff=handoff,
                result=StageResult(
                    stage=SelfOperationStage.ALIGNMENT,
                    handoff_digest=handoff.digest,
                    reason=reason,
                    output_digest=None,
                ),
                route=AlignmentRoute.STOP.value,
                allowed_writes=(),
                stop_conditions=self._STOP_CONDITIONS,
            ),
            route=AlignmentRoute.STOP,
            selected_candidate_id=None,
            objective_id=None,
            output=None,
            assessments=assessments,
        )
