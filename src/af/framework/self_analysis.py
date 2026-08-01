"""Pure planning contracts for AF's governed self-analysis stage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

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
    _iso8601,
)

_MAX_CANDIDATES = 1_000
_MAX_METRIC = 10_000
_MAX_SNAPSHOT_AGE_SECONDS = 86_400
_MAX_STATEMENTS = 64
_MAX_STATEMENT_LENGTH = 400


class AnalysisFocusKind(StrEnum):
    """Bounded analysis targets understood by the first stage slice."""

    GAP = "gap"
    REGRESSION = "regression"
    CONFLICT = "conflict"
    DEPENDENCY = "dependency"


class AnalysisRoute(StrEnum):
    """The only routes emitted by the read-only analysis stage."""

    STOP = "stop"
    ANALYSE = "analyse"


def _canonical_statements(value: object, field_name: str) -> tuple[str, ...]:
    if type(value) is not tuple or not 1 <= len(value) <= _MAX_STATEMENTS:
        raise ValueError(f"self-analysis {field_name} are invalid")
    normalized: list[str] = []
    for item in value:
        if type(item) is not str:
            raise ValueError(f"self-analysis {field_name} are invalid")
        stripped = item.strip()
        if not stripped or len(stripped) > _MAX_STATEMENT_LENGTH:
            raise ValueError(f"self-analysis {field_name} are invalid")
        normalized.append(stripped)
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"self-analysis {field_name} are invalid")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class AnalysisCandidate:
    """One immutable analysis candidate and its fail-closed eligibility facts."""

    candidate_id: str
    revision: str
    focus_kind: AnalysisFocusKind
    priority: int
    urgency: int
    risk: int
    created_at: datetime
    facts: tuple[str, ...]
    hypotheses: tuple[str, ...]
    uncertainties: tuple[str, ...]
    causal_claims: tuple[str, ...]
    clarification_needs: tuple[str, ...]
    active_overlap: bool
    evidence_complete: bool
    authority_granted: bool
    complete: bool

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not str or _CANDIDATE_ID.fullmatch(self.candidate_id) is None:
            raise ValueError("self-analysis candidate identity is invalid")
        if type(self.revision) is not str or _SHA256.fullmatch(self.revision) is None:
            raise ValueError("self-analysis candidate revision is invalid")
        if not isinstance(self.focus_kind, AnalysisFocusKind):
            raise ValueError("self-analysis focus kind is invalid")
        object.__setattr__(
            self,
            "priority",
            _bounded_int(self.priority, "priority", maximum=9),
        )
        object.__setattr__(
            self,
            "urgency",
            _bounded_int(self.urgency, "urgency", maximum=_MAX_METRIC),
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
        object.__setattr__(self, "facts", _canonical_statements(self.facts, "facts"))
        object.__setattr__(
            self,
            "hypotheses",
            _canonical_statements(self.hypotheses, "hypotheses"),
        )
        object.__setattr__(
            self,
            "uncertainties",
            _canonical_statements(self.uncertainties, "uncertainties"),
        )
        object.__setattr__(
            self,
            "causal_claims",
            _canonical_statements(self.causal_claims, "causal claims"),
        )
        object.__setattr__(
            self,
            "clarification_needs",
            _canonical_statements(self.clarification_needs, "clarification needs"),
        )
        boolean_fields = (
            self.active_overlap,
            self.evidence_complete,
            self.authority_granted,
            self.complete,
        )
        if any(type(value) is not bool for value in boolean_fields):
            raise ValueError("self-analysis eligibility facts must be boolean")

    def _payload(self) -> dict[str, object]:
        return {
            "active_overlap": self.active_overlap,
            "authority_granted": self.authority_granted,
            "candidate_id": self.candidate_id,
            "causal_claims": list(self.causal_claims),
            "clarification_needs": list(self.clarification_needs),
            "complete": self.complete,
            "created_at": _iso8601(self.created_at),
            "evidence_complete": self.evidence_complete,
            "facts": list(self.facts),
            "focus_kind": self.focus_kind.value,
            "hypotheses": list(self.hypotheses),
            "priority": self.priority,
            "revision": self.revision,
            "risk": self.risk,
            "uncertainties": list(self.uncertainties),
            "urgency": self.urgency,
        }


@dataclass(frozen=True, slots=True)
class AnalysisSnapshot:
    """One exact, bounded analysis observation used for a single plan."""

    operation: SelfOperationSnapshot
    candidates: tuple[AnalysisCandidate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.operation, SelfOperationSnapshot):
            raise ValueError("self-analysis operation snapshot is invalid")
        if (
            type(self.candidates) is not tuple
            or len(self.candidates) > _MAX_CANDIDATES
            or any(not isinstance(item, AnalysisCandidate) for item in self.candidates)
        ):
            raise ValueError("self-analysis candidates are invalid")
        ordered = tuple(sorted(self.candidates, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in ordered}) != len(ordered):
            raise ValueError("self-analysis candidate identities must be unique")
        object.__setattr__(self, "candidates", ordered)

    def canonical_json(self) -> str:
        """Return the byte-stable analysis snapshot representation."""
        return _canonical_json(
            {
                "candidates": [item._payload() for item in self.candidates],
                "operation": self.operation._payload(),
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class AnalysisPolicy:
    """Explicit policy inputs that influence deterministic analysis selection."""

    max_snapshot_age_seconds: int
    tie_break_by_id: bool
    entry_authority_id: str

    def __post_init__(self) -> None:
        if (
            type(self.max_snapshot_age_seconds) is not int
            or not 1 <= self.max_snapshot_age_seconds <= _MAX_SNAPSHOT_AGE_SECONDS
        ):
            raise ValueError("self-analysis snapshot age policy is invalid")
        if type(self.tie_break_by_id) is not bool:
            raise ValueError("self-analysis tie-break policy is invalid")
        if not _is_trace_id(self.entry_authority_id):
            raise ValueError("self-analysis entry authority is invalid")

    def canonical_json(self) -> str:
        """Return the byte-stable policy representation bound into the plan."""
        return _canonical_json(
            {
                "entry_authority_id": self.entry_authority_id,
                "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
                "tie_break_by_id": self.tie_break_by_id,
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class AnalysisAssessment:
    """The eligibility result for one observed analysis candidate."""

    candidate_id: str
    reason: StageReason

    def __post_init__(self) -> None:
        if type(self.candidate_id) is not str or _CANDIDATE_ID.fullmatch(self.candidate_id) is None:
            raise ValueError("self-analysis assessment identity is invalid")
        if not isinstance(self.reason, StageReason):
            raise ValueError("self-analysis assessment reason is invalid")

    def _payload(self) -> dict[str, str]:
        return {"candidate_id": self.candidate_id, "reason": self.reason.value}


@dataclass(frozen=True, slots=True)
class AnalysisOutput:
    """The canonical read-only output emitted by a successful analysis stage."""

    focus_kind: AnalysisFocusKind
    facts: tuple[str, ...]
    hypotheses: tuple[str, ...]
    uncertainties: tuple[str, ...]
    causal_claims: tuple[str, ...]
    clarification_needs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.focus_kind, AnalysisFocusKind):
            raise ValueError("self-analysis output focus kind is invalid")
        object.__setattr__(self, "facts", _canonical_statements(self.facts, "facts"))
        object.__setattr__(
            self,
            "hypotheses",
            _canonical_statements(self.hypotheses, "hypotheses"),
        )
        object.__setattr__(
            self,
            "uncertainties",
            _canonical_statements(self.uncertainties, "uncertainties"),
        )
        object.__setattr__(
            self,
            "causal_claims",
            _canonical_statements(self.causal_claims, "causal claims"),
        )
        object.__setattr__(
            self,
            "clarification_needs",
            _canonical_statements(self.clarification_needs, "clarification needs"),
        )

    def _payload(self) -> dict[str, object]:
        return {
            "causal_claims": list(self.causal_claims),
            "clarification_needs": list(self.clarification_needs),
            "facts": list(self.facts),
            "focus_kind": self.focus_kind.value,
            "hypotheses": list(self.hypotheses),
            "uncertainties": list(self.uncertainties),
        }

    def canonical_json(self) -> str:
        """Return the byte-stable analysis output representation."""
        return _canonical_json(self._payload())

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


@dataclass(frozen=True, slots=True)
class AnalysisPlan:
    """A canonical read-only self-analysis plan bound to one exact stage plan."""

    stage_plan: StagePlan
    route: AnalysisRoute
    selected_candidate_id: str | None
    focus_kind: AnalysisFocusKind | None
    output: AnalysisOutput | None
    assessments: tuple[AnalysisAssessment, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.stage_plan, StagePlan):
            raise ValueError("self-analysis stage plan is invalid")
        if self.stage_plan.stage is not SelfOperationStage.ANALYSIS:
            raise ValueError("self-analysis stage plan stage is invalid")
        if not isinstance(self.route, AnalysisRoute):
            raise ValueError("self-analysis route is invalid")
        if self.selected_candidate_id is not None and (
            type(self.selected_candidate_id) is not str
            or _CANDIDATE_ID.fullmatch(self.selected_candidate_id) is None
        ):
            raise ValueError("self-analysis selected identity is invalid")
        if self.focus_kind is not None and not isinstance(self.focus_kind, AnalysisFocusKind):
            raise ValueError("self-analysis selected focus kind is invalid")
        if self.output is not None and not isinstance(self.output, AnalysisOutput):
            raise ValueError("self-analysis output is invalid")
        if type(self.assessments) is not tuple or any(
            not isinstance(item, AnalysisAssessment) for item in self.assessments
        ):
            raise ValueError("self-analysis assessments are invalid")
        ordered = tuple(sorted(self.assessments, key=lambda item: item.candidate_id))
        if len({item.candidate_id for item in ordered}) != len(ordered):
            raise ValueError("self-analysis assessment identities must be unique")
        object.__setattr__(self, "assessments", ordered)
        if self.route is AnalysisRoute.STOP:
            if (
                self.selected_candidate_id is not None
                or self.focus_kind is not None
                or self.output is not None
                or self.stage_plan.route != "stop"
            ):
                raise ValueError("self-analysis stop plan is inconsistent")
        else:
            if (
                self.selected_candidate_id is None
                or self.focus_kind is None
                or self.output is None
                or self.stage_plan.route != "analyse"
                or self.stage_plan.result.output_digest != self.output.digest
            ):
                raise ValueError("self-analysis active plan is inconsistent")

    def canonical_json(self) -> str:
        """Return the deterministic serialized plan suitable for replay."""
        return _canonical_json(
            {
                "assessments": [item._payload() for item in self.assessments],
                "causal_claims": (
                    list(self.output.causal_claims) if self.output is not None else None
                ),
                "clarification_needs": (
                    list(self.output.clarification_needs) if self.output is not None else None
                ),
                "facts": list(self.output.facts) if self.output is not None else None,
                "focus_kind": self.focus_kind.value if self.focus_kind is not None else None,
                "hypotheses": list(self.output.hypotheses) if self.output is not None else None,
                "route": self.route.value,
                "selected_candidate_id": self.selected_candidate_id,
                "stage_plan": self.stage_plan._payload(),
                "uncertainties": (
                    list(self.output.uncertainties) if self.output is not None else None
                ),
            }
        )

    @property
    def digest(self) -> str:
        return _digest(self.canonical_json())


class SelfAnalysisPlanner:
    """Select at most one eligible analysis input and emit a read-only plan."""

    _STOP_CONDITIONS = (
        "authority_missing",
        "cancelled",
        "evidence_missing",
        "overlap_detected",
        "snapshot_stale",
        "unsafe",
    )

    def plan(
        self,
        snapshot: AnalysisSnapshot,
        policy: AnalysisPolicy,
        *,
        now: datetime,
    ) -> AnalysisPlan:
        if not isinstance(snapshot, AnalysisSnapshot):
            raise TypeError("snapshot must be an AnalysisSnapshot")
        if not isinstance(policy, AnalysisPolicy):
            raise TypeError("policy must be an AnalysisPolicy")
        observed_now = _aware_utc(now, "planning time")
        if observed_now < snapshot.operation.captured_at:
            raise ValueError("self-analysis planning time precedes snapshot")

        entry_authority = self._entry_authority(
            snapshot.operation,
            policy.entry_authority_id,
        )
        if entry_authority is None or not entry_authority.granted:
            assessments = tuple(
                AnalysisAssessment(item.candidate_id, StageReason.MISSING_AUTHORITY)
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
                AnalysisAssessment(item.candidate_id, StageReason.STALE_SNAPSHOT)
                for item in snapshot.candidates
            )
            return self._stop_plan(
                snapshot,
                policy,
                assessments=assessments,
                reason=StageReason.STALE_SNAPSHOT,
            )

        assessments = tuple(
            AnalysisAssessment(item.candidate_id, self._eligibility_reason(item))
            for item in snapshot.candidates
        )
        reasons = {item.candidate_id: item.reason for item in assessments}
        eligible = tuple(
            item
            for item in snapshot.candidates
            if reasons[item.candidate_id] is StageReason.SELECTED
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
            stage=SelfOperationStage.ANALYSIS,
            source_snapshot_digest=snapshot.operation.digest,
            input_artifact_digest=snapshot.digest,
            policy_digest=policy.digest,
            predecessor_result_digest=None,
            entry_authority_id=policy.entry_authority_id,
        )
        output = AnalysisOutput(
            focus_kind=selected.focus_kind,
            facts=selected.facts,
            hypotheses=selected.hypotheses,
            uncertainties=selected.uncertainties,
            causal_claims=selected.causal_claims,
            clarification_needs=selected.clarification_needs,
        )
        stage_plan = StagePlan(
            stage=SelfOperationStage.ANALYSIS,
            handoff=handoff,
            result=StageResult(
                stage=SelfOperationStage.ANALYSIS,
                handoff_digest=handoff.digest,
                reason=StageReason.SELECTED,
                output_digest=output.digest,
            ),
            route=AnalysisRoute.ANALYSE.value,
            allowed_writes=(),
            stop_conditions=self._STOP_CONDITIONS,
        )
        return AnalysisPlan(
            stage_plan=stage_plan,
            route=AnalysisRoute.ANALYSE,
            selected_candidate_id=selected.candidate_id,
            focus_kind=selected.focus_kind,
            output=output,
            assessments=assessments,
        )

    @staticmethod
    def _entry_authority(
        snapshot: SelfOperationSnapshot,
        authority_id: str,
    ) -> AuthorityRecord | None:
        return next(
            (item for item in snapshot.authorities if item.authority_id == authority_id),
            None,
        )

    @staticmethod
    def _eligibility_reason(candidate: AnalysisCandidate) -> StageReason:
        if candidate.complete:
            return StageReason.COMPLETE
        if candidate.active_overlap:
            return StageReason.ACTIVE_OVERLAP
        if not candidate.evidence_complete:
            return StageReason.MISSING_EVIDENCE
        if not candidate.authority_granted:
            return StageReason.MISSING_AUTHORITY
        return StageReason.SELECTED

    @staticmethod
    def _order_key(candidate: AnalysisCandidate) -> tuple[int, int, int, datetime]:
        return (candidate.priority, -candidate.urgency, candidate.risk, candidate.created_at)

    def _stop_plan(
        self,
        snapshot: AnalysisSnapshot,
        policy: AnalysisPolicy,
        *,
        assessments: tuple[AnalysisAssessment, ...],
        reason: StageReason,
    ) -> AnalysisPlan:
        handoff = StageHandoff(
            stage=SelfOperationStage.ANALYSIS,
            source_snapshot_digest=snapshot.operation.digest,
            input_artifact_digest=snapshot.digest,
            policy_digest=policy.digest,
            predecessor_result_digest=None,
            entry_authority_id=policy.entry_authority_id,
        )
        return AnalysisPlan(
            stage_plan=StagePlan(
                stage=SelfOperationStage.ANALYSIS,
                handoff=handoff,
                result=StageResult(
                    stage=SelfOperationStage.ANALYSIS,
                    handoff_digest=handoff.digest,
                    reason=reason,
                    output_digest=None,
                ),
                route=AnalysisRoute.STOP.value,
                allowed_writes=(),
                stop_conditions=self._STOP_CONDITIONS,
            ),
            route=AnalysisRoute.STOP,
            selected_candidate_id=None,
            focus_kind=None,
            output=None,
            assessments=assessments,
        )
