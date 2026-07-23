"""Inquiry state and deterministic ask/answer/stop rules.

All eleven questions are structurally present in every InquiryState with
evidence-driven deterministic behaviour:

- WHO   — satisfied when every required who-typed role declared by viable candidates
          has a corresponding context field supplied (context.actor for agent,
          context.subject for subject, context.experiencer for experiencer, etc.);
          open (material) when a viable candidate carries an unfilled who-typed role;
          not-applicable otherwise.  WHO remains open until ALL required roles are
          resolved, not only the first one found.
- WHAT  — satisfied when every word-form has at least one viable dictionary candidate;
          open (material) when any word-form has no viable candidate.
- WHERE — satisfied when exactly one location candidate is viable; contested (material)
          when multiple candidates remain; not-applicable when none are present.
- WHEN  — satisfied when a valid temporal or conditional anchor is declared:
          context.time must be a parseable, timezone-aware ISO 8601 timestamp;
          context.when must follow the bounded ``kind:expression`` format where kind is
          one of ``event``, ``criterion``, ``interval``, or ``ordering``.  All fields
          fail closed: an invalid or arbitrarily-formed string does not satisfy WHEN.
          Parsing is delegated to ``temporal.parse_when_expression`` (public API).
- WHERN — composite WHERE + WHEN.  Satisfied only when WHERE is fully satisfied and
          WHEN is satisfied.  Contested when WHERE is contested and WHEN is satisfied.
          Open (non-material) with missing role identified when exactly one component
          is available or when WHERE is contested but WHEN is missing.  Not-applicable
          when neither component is present.
- IS    — satisfied when at least one predicate (action, state, relation) is present;
          open (material) when none are found.
- WHY   — open (non-material) when a predicate is present that could have a causal
          explanation; not-applicable otherwise.
- HOW   — open (non-material) when a predicate is present that could have a method or
          manner role; not-applicable otherwise.
- COULD — open (non-material) when both a predicate and a declared actor are present;
          not-applicable otherwise.
- WOULD — open (non-material) when a predicate is present that could be qualified
          hypothetically or conditionally; not-applicable otherwise.
- SHOULD — satisfied when a ShouldAssessment is provided.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from af.domain.ideons.records import (
    QUESTIONS,
    CandidateSet,
    ContextGraph,
    Decision,
    DecisionTrace,
    FormSequence,
    InquiryResult,
    InquiryState,
    InquiryStatus,
    Question,
    ShouldAssessment,
)
from af.domain.ideons.temporal import parse_when_expression

_WHO_TYPED_ROLES: frozenset[str] = frozenset(
    {"agent", "subject", "experiencer", "source", "possessor", "affected"}
)

# Maps semantic role names (from SenseEntry.semantic_roles) to the context field
# that satisfies them.  Only roles present in this map participate in WHO resolution.
_WHO_ROLE_CONTEXT_MAP: dict[str, str] = {
    "agent": "actor",
    "subject": "subject",
    "experiencer": "experiencer",
    "source": "source",
    "possessor": "possessor",
    "affected": "affected",
}


def _is_valid_time_string(value: str) -> bool:
    """Return True if *value* is a parseable, timezone-aware ISO 8601 timestamp.

    Fail-closed: any non-parseable string or naive datetime returns False so that
    WHEN is not satisfied merely because context.time is a non-empty string.
    """
    try:
        dt = datetime.fromisoformat(value)
        return dt.tzinfo is not None
    except (ValueError, OverflowError):
        return False


class InquiryEngine:
    def query(
        self,
        forms: FormSequence,
        candidates: tuple[CandidateSet, ...],
        context: ContextGraph,
        *,
        should: ShouldAssessment | None = None,
    ) -> InquiryState:
        results = {
            question: InquiryResult(question, InquiryStatus.NOT_APPLICABLE)
            for question in QUESTIONS
        }

        word_candidate_sets = tuple(
            candidate_set
            for index, candidate_set in enumerate(candidates)
            if forms.forms[index].form_type == "word-form"
        )

        # Compute predicate presence upfront: drives IS, WHY, HOW, COULD, WOULD.
        has_predicate = any(
            candidate.entry.semantic_type in {"action", "state", "relation"}
            for candidate_set in word_candidate_sets
            for candidate in candidate_set.viable
        )

        # Collect who-typed roles required by viable candidates.
        # WHO is material-open only when a required who-typed role is declared on a
        # viable candidate but not satisfied by any corresponding context field.
        required_who_roles: set[str] = {
            role
            for candidate_set in word_candidate_sets
            for candidate in candidate_set.viable
            for role in candidate.entry.semantic_roles
            if role in _WHO_TYPED_ROLES
        }
        has_who_typed_role = bool(required_who_roles)

        # Keep actor separately for COULD (capability assessment is agent-specific).
        actor = context.get("actor")

        # WHO: check ALL required who-typed roles — WHO remains open until every declared
        # role has a corresponding context value, not only the first one found.
        who_satisfied_pairs: list[tuple[str, str]] = []  # (context_field, value)
        who_missing_fields: list[str] = []  # context fields with no supplied value

        for semantic_role, context_field in _WHO_ROLE_CONTEXT_MAP.items():
            if semantic_role not in required_who_roles:
                continue
            ctx_val = context.get(context_field)
            if ctx_val:
                who_satisfied_pairs.append((context_field, ctx_val))
            else:
                who_missing_fields.append(context_field)

        if has_who_typed_role:
            if not who_missing_fields:
                # Every required who-typed role is satisfied.
                primary_field, _ = who_satisfied_pairs[0]
                results[Question.WHO] = InquiryResult(
                    Question.WHO,
                    InquiryStatus.SATISFIED,
                    role=primary_field,
                    evidence=tuple(f"context:{field}={val}" for field, val in who_satisfied_pairs),
                )
            else:
                # One or more required who-typed roles are missing.
                first_missing = who_missing_fields[0]
                results[Question.WHO] = InquiryResult(
                    Question.WHO,
                    InquiryStatus.OPEN,
                    role=first_missing,
                    required_information=f"the {first_missing} of the utterance",
                    material=True,
                    evidence=tuple(f"missing:{f}" for f in who_missing_fields),
                )
        # else: leave WHO as NOT_APPLICABLE (impersonal, no predicate, or no who-typed role)

        # WHAT
        unknown = tuple(
            candidate_set for candidate_set in word_candidate_sets if not candidate_set.viable
        )
        results[Question.WHAT] = InquiryResult(
            Question.WHAT,
            InquiryStatus.OPEN if unknown else InquiryStatus.SATISFIED,
            role="sense",
            required_information=(
                "dictionary senses for " + ", ".join(item.surface for item in unknown)
                if unknown
                else None
            ),
            material=bool(unknown),
            evidence=tuple(f"unknown-form:{item.surface}" for item in unknown),
        )

        # WHERE
        location_candidates = tuple(
            candidate
            for candidate_set in word_candidate_sets
            for candidate in candidate_set.viable
            if candidate.entry.question_slot is Question.WHERE
            and candidate.entry.semantic_type in {"entity", "place"}
        )
        location_keys = tuple(candidate.entry.ideon_key for candidate in location_candidates)
        if len(location_candidates) > 1:
            results[Question.WHERE] = InquiryResult(
                Question.WHERE,
                InquiryStatus.CONTESTED,
                role="location-sense",
                candidate_keys=location_keys,
                required_information="the contextual set that distinguishes location senses",
                material=True,
                evidence=tuple(
                    f"candidate:{candidate.entry.entry_id}" for candidate in location_candidates
                ),
            )
        elif location_candidates:
            results[Question.WHERE] = InquiryResult(
                Question.WHERE,
                InquiryStatus.SATISFIED,
                role="location-sense",
                candidate_keys=location_keys,
                evidence=(f"selected:{location_candidates[0].entry.entry_id}",),
            )

        # WHEN: condition-bearing temporal relation (timestamp, interval, event, criterion,
        # ordering).
        # context.time must be a parseable, timezone-aware ISO timestamp — fail-closed.
        # context.when must follow the bounded ``kind:expression`` format — fail-closed.
        # Arbitrary non-empty strings do not satisfy WHEN.
        time_val = context.get("time")
        when_val = context.get("when")
        time_valid = time_val is not None and _is_valid_time_string(time_val)
        when_condition = when_val is not None and parse_when_expression(when_val) is not None
        when_satisfied = time_valid or when_condition
        t_ev: tuple[str, ...] = tuple(
            v
            for v in (
                f"context:time={time_val}" if time_valid else None,
                f"context:when={when_val}" if when_condition else None,
            )
            if v is not None
        )
        if when_satisfied:
            results[Question.WHEN] = InquiryResult(
                Question.WHEN,
                InquiryStatus.SATISFIED,
                role="temporal-condition",
                evidence=t_ev,
            )

        # WHERN: composite WHERE (spatial/domain) + WHEN (temporal/conditional).
        # Satisfied only when WHERE is fully satisfied and WHEN is satisfied.
        # WHERE CONTESTED + WHEN SATISFIED → WHERN CONTESTED (contested status propagates).
        # WHERE present but WHEN missing → WHERN OPEN (non-material).
        # WHERE absent + WHEN SATISFIED → WHERN OPEN (non-material).
        # Neither present → WHERN NOT_APPLICABLE (default).
        where_context = context.get("where")
        where_result_status = results[Question.WHERE].status
        where_satisfied_from_candidates = where_result_status is InquiryStatus.SATISFIED
        where_contested_from_candidates = where_result_status is InquiryStatus.CONTESTED
        where_context_present = where_context is not None
        where_fully_satisfied = where_satisfied_from_candidates or where_context_present
        where_present = where_fully_satisfied or where_contested_from_candidates

        if where_fully_satisfied and when_satisfied:
            w_ev = (
                (f"context:where={where_context}",)
                if where_context_present
                else ("where:from-candidates",)
            )
            results[Question.WHERN] = InquiryResult(
                Question.WHERN,
                InquiryStatus.SATISFIED,
                role="spatiotemporal-condition",
                evidence=w_ev + t_ev,
            )
        elif where_contested_from_candidates and when_satisfied:
            # WHERE is contested; WHERN inherits the contested status.
            results[Question.WHERN] = InquiryResult(
                Question.WHERN,
                InquiryStatus.CONTESTED,
                role="spatiotemporal-condition",
                required_information="unambiguous location for WHERN",
                material=True,
                evidence=("where:contested", *t_ev),
            )
        elif where_present and not when_satisfied:
            # WHERE is present (fully or contested) but WHEN is missing.
            where_ev = ("where:contested",) if where_contested_from_candidates else ()
            results[Question.WHERN] = InquiryResult(
                Question.WHERN,
                InquiryStatus.OPEN,
                role="spatiotemporal-condition",
                required_information="temporal condition for WHERN",
                material=False,
                evidence=(*where_ev, "missing:when"),
            )
        elif when_satisfied and not where_present:
            results[Question.WHERN] = InquiryResult(
                Question.WHERN,
                InquiryStatus.OPEN,
                role="spatiotemporal-condition",
                required_information="spatial or domain location for WHERN",
                material=False,
                evidence=("missing:where",),
            )

        # IS
        results[Question.IS] = InquiryResult(
            Question.IS,
            InquiryStatus.SATISFIED if has_predicate else InquiryStatus.OPEN,
            role="predicate",
            required_information=None if has_predicate else "an action, state, or relation",
            material=not has_predicate,
            evidence=("predicate-present",) if has_predicate else ("missing:predicate",),
        )

        # WHY: open (non-material) when a predicate is present that could have a causal
        # explanation; not-applicable otherwise.
        if has_predicate:
            results[Question.WHY] = InquiryResult(
                Question.WHY,
                InquiryStatus.OPEN,
                role="causal-explanation",
                material=False,
                evidence=("predicate-present",),
            )

        # HOW: open (non-material) when a predicate is present that could have a
        # method/manner role; not-applicable otherwise.
        if has_predicate:
            results[Question.HOW] = InquiryResult(
                Question.HOW,
                InquiryStatus.OPEN,
                role="method-or-manner",
                material=False,
                evidence=("predicate-present",),
            )

        # COULD: open (non-material) when both a predicate and a declared actor are
        # present; capability assessment requires an identified agent.
        if has_predicate and actor:
            results[Question.COULD] = InquiryResult(
                Question.COULD,
                InquiryStatus.OPEN,
                role="capability-assessment",
                material=False,
                evidence=("predicate-present", f"context:actor={actor}"),
            )

        # WOULD: open (non-material) when a predicate is present that could be qualified
        # hypothetically or conditionally; not-applicable otherwise.
        if has_predicate:
            results[Question.WOULD] = InquiryResult(
                Question.WOULD,
                InquiryStatus.OPEN,
                role="hypothetical-or-conditional",
                material=False,
                evidence=("predicate-present",),
            )

        # SHOULD
        if should is not None:
            results[Question.SHOULD] = InquiryResult(
                Question.SHOULD,
                InquiryStatus.SATISFIED,
                role="normative-assessment",
                evidence=(
                    f"relevance={should.relevance}",
                    f"severity={should.severity}",
                    f"confidence={should.confidence_or_margin}",
                ),
            )
        return InquiryState(tuple(results[question] for question in QUESTIONS))


class DecisionEngine:
    def __init__(self, stop_threshold: Decimal = Decimal("0.64")) -> None:
        if stop_threshold < Decimal(0) or stop_threshold > Decimal(1):
            raise ValueError("stop threshold must be within [0, 1]")
        self._stop_threshold = stop_threshold

    @property
    def policy(self) -> tuple[tuple[str, str], ...]:
        return (("should-stop-threshold", str(self._stop_threshold)),)

    def decide(
        self,
        inquiries: InquiryState,
        *,
        should: ShouldAssessment | None = None,
        action_requested: bool = False,
        media_complete: bool = True,
    ) -> DecisionTrace:
        if should is not None:
            expected_harm = should.relevance * should.severity * should.confidence_or_margin
            if should.forbidden or expected_harm >= self._stop_threshold:
                if should.recommended_alternative is not None:
                    return DecisionTrace(
                        Decision.ASK,
                        ("should.safer-alternative",),
                        (f"expected-harm={expected_harm}",),
                        question=Question.SHOULD,
                        safer_alternative=should.recommended_alternative,
                        policy=self.policy,
                    )
                return DecisionTrace(
                    Decision.STOP,
                    ("should.stop-threshold",),
                    (f"expected-harm={expected_harm}",),
                    question=Question.SHOULD,
                    policy=self.policy,
                )
        if inquiries.material_openings:
            first = inquiries.material_openings[0]
            return DecisionTrace(
                Decision.ASK,
                ("closure.material-opening",),
                tuple(
                    f"{opening.question.value}:{opening.role or 'unspecified'}"
                    for opening in inquiries.material_openings
                ),
                question=first.question,
                policy=self.policy,
            )
        if not media_complete:
            return DecisionTrace(
                Decision.CONTINUE_READING,
                ("media.incomplete",),
                policy=self.policy,
            )
        if action_requested:
            return DecisionTrace(
                Decision.ACT,
                ("closure.satisfied",),
                policy=self.policy,
            )
        return DecisionTrace(
            Decision.ANSWER,
            ("closure.satisfied",),
            policy=self.policy,
        )
