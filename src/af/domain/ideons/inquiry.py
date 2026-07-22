"""Inquiry state and deterministic ask/answer/stop rules.

Eleven questions are structurally present in every InquiryState.  Deterministic
behaviour is implemented for WHO, WHAT, WHERE, WHEN, WHERN, IS, and SHOULD.
WHY, HOW, COULD, and WOULD default to NOT_APPLICABLE until their respective
procedural rules are added; they are represented but not yet evaluated.
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

        # Compute predicate presence upfront: it drives both IS and WHO evaluation.
        # WHO is material-open only when a predicate requires an agent; IS is open
        # when no predicate is found.  Computing this once avoids two passes.
        has_predicate = any(
            candidate.entry.semantic_type in {"action", "state", "relation"}
            for candidate_set in word_candidate_sets
            for candidate in candidate_set.viable
        )

        # WHO: only materially open when a predicate is present that requires an agent.
        # Distinguish: agent present → SATISFIED; predicate present but no agent → OPEN
        # (material); no predicate → NOT_APPLICABLE (nothing requires an actor).
        actor = context.get("actor")
        if actor:
            results[Question.WHO] = InquiryResult(
                Question.WHO,
                InquiryStatus.SATISFIED,
                role="actor",
                evidence=(f"context:actor={actor}",),
            )
        elif has_predicate:
            results[Question.WHO] = InquiryResult(
                Question.WHO,
                InquiryStatus.OPEN,
                role="actor",
                required_information="the actor inherited by the utterance",
                material=True,
                evidence=("missing:actor",),
            )
        # else: leave WHO as NOT_APPLICABLE (default)

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

        if context.get("time") is not None:
            results[Question.WHEN] = InquiryResult(
                Question.WHEN,
                InquiryStatus.SATISFIED,
                role="temporal-anchor",
                evidence=(f"context:time={context.get('time')}",),
            )
        if context.get("criterion") is not None:
            results[Question.WHERN] = InquiryResult(
                Question.WHERN,
                InquiryStatus.SATISFIED,
                role="criterion",
                evidence=(f"context:criterion={context.get('criterion')}",),
            )

        results[Question.IS] = InquiryResult(
            Question.IS,
            InquiryStatus.SATISFIED if has_predicate else InquiryStatus.OPEN,
            role="predicate",
            required_information=None if has_predicate else "an action, state, or relation",
            material=not has_predicate,
            evidence=("predicate-present",) if has_predicate else ("missing:predicate",),
        )
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
