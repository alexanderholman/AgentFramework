"""Inquiry state and deterministic ask/answer/stop rules.

All eleven questions are structurally present in every InquiryState with
evidence-driven deterministic behaviour:

- WHO   — satisfied when context actor is declared; open (material) when a viable
          candidate carries an unfilled who-typed role (agent, subject, experiencer,
          source, possessor, affected); not-applicable otherwise.
- WHAT  — satisfied when every word-form has at least one viable dictionary candidate;
          open (material) when any word-form has no viable candidate.
- WHERE — satisfied when exactly one location candidate is viable; contested (material)
          when multiple candidates remain; not-applicable when none are present.
- WHEN  — satisfied when a temporal or conditional anchor is declared (context.time for
          a timestamp witness; context.when for an event/criterion expression); not-
          applicable otherwise.
- WHERN — composite WHERE + WHEN.  Satisfied when both the spatial/domain component
          (WHERE satisfied or context.where declared) and the temporal/conditional
          component (WHEN satisfied) are present.  Open (non-material) with the
          missing role identified when only one component is available.
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

_WHO_TYPED_ROLES: frozenset[str] = frozenset(
    {"agent", "subject", "experiencer", "source", "possessor", "affected"}
)


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

        # Compute who-typed role presence: drives WHO.
        # WHO is material-open only when a viable candidate has an unfilled who-typed
        # role (agent, subject, experiencer, source, possessor, affected).  An
        # impersonal state/process with no who-typed roles leaves WHO NOT_APPLICABLE
        # even when a predicate is present.
        has_who_typed_role = any(
            bool(_WHO_TYPED_ROLES.intersection(candidate.entry.semantic_roles))
            for candidate_set in word_candidate_sets
            for candidate in candidate_set.viable
        )

        # WHO
        actor = context.get("actor")
        if actor:
            results[Question.WHO] = InquiryResult(
                Question.WHO,
                InquiryStatus.SATISFIED,
                role="actor",
                evidence=(f"context:actor={actor}",),
            )
        elif has_who_typed_role:
            results[Question.WHO] = InquiryResult(
                Question.WHO,
                InquiryStatus.OPEN,
                role="actor",
                required_information="the actor inherited by the utterance",
                material=True,
                evidence=("missing:actor",),
            )
        # else: leave WHO as NOT_APPLICABLE (impersonal or no predicate)

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

        # WHEN: condition-bearing temporal relation (timestamp, interval, event, criterion).
        # Satisfied by context.time (timestamp witness) or context.when (event/criterion
        # expression).  A timestamp is one possible witness; WHEN is not a scalar datetime.
        time_val = context.get("time")
        when_val = context.get("when")
        when_satisfied = bool(time_val or when_val)
        if when_satisfied:
            when_evidence: tuple[str, ...] = tuple(
                v
                for v in (
                    f"context:time={time_val}" if time_val else None,
                    f"context:when={when_val}" if when_val else None,
                )
                if v is not None
            )
            results[Question.WHEN] = InquiryResult(
                Question.WHEN,
                InquiryStatus.SATISFIED,
                role="temporal-condition",
                evidence=when_evidence,
            )

        # WHERN: composite WHERE (spatial/domain) + WHEN (temporal/conditional).
        # Satisfied only when both components are present.  Open (non-material) with the
        # missing role identified when only one component is available.
        where_context = context.get("where")
        where_component = (
            results[Question.WHERE].status is InquiryStatus.SATISFIED
            or where_context is not None
        )
        if where_component and when_satisfied:
            w_ev = (
                (f"context:where={where_context}",)
                if where_context
                else ("where:from-candidates",)
            )
            t_ev: tuple[str, ...] = tuple(
                v
                for v in (
                    f"context:time={time_val}" if time_val else None,
                    f"context:when={when_val}" if when_val else None,
                )
                if v is not None
            )
            results[Question.WHERN] = InquiryResult(
                Question.WHERN,
                InquiryStatus.SATISFIED,
                role="spatiotemporal-condition",
                evidence=w_ev + t_ev,
            )
        elif where_component:
            results[Question.WHERN] = InquiryResult(
                Question.WHERN,
                InquiryStatus.OPEN,
                role="spatiotemporal-condition",
                required_information="temporal condition for WHERN",
                material=False,
                evidence=("missing:when",),
            )
        elif when_satisfied:
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
