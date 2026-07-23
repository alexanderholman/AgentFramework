from __future__ import annotations

import unicodedata
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from af.domain.ideons.dictionary import LogicDictionary
from af.domain.ideons.forms import LosslessFormEncoder
from af.domain.ideons.inquiry import DecisionEngine, InquiryEngine
from af.domain.ideons.records import (
    QUESTIONS,
    ContextGraph,
    Decision,
    InquiryStatus,
    LanguageProfileRef,
    Question,
    SenseEntry,
    ShouldAssessment,
)
from af.domain.ideons.temporal import (
    AuxiliaryKind,
    TemporalQualification,
    Tense,
    WhenCondition,
    WhenKind,
    auxiliary_kind,
    render_tense,
)

REVISION = "a" * 64
PROFILE = LanguageProfileRef("en", "0.1", unicodedata.unidata_version, dialect="GB")


def entry(
    entry_id: str,
    key: int,
    form: str,
    semantic_type: str,
    slot: Question,
    *,
    valid_contexts: tuple[tuple[str, str], ...] = (),
    semantic_roles: tuple[str, ...] = (),
) -> SenseEntry:
    return SenseEntry(
        entry_id=entry_id,
        ideon_key=key,
        ideon_revision=REVISION,
        profile_id=PROFILE.identifier,
        forms=(form,),
        lexeme=entry_id.split(".")[0],
        sense=entry_id,
        semantic_type=semantic_type,
        question_slot=slot,
        valid_contexts=valid_contexts,
        semantic_roles=semantic_roles,
    )


def reference_dictionary() -> LogicDictionary:
    return LogicDictionary(
        (
            entry("sit.past", 2, "sat", "action", Question.WHAT, semantic_roles=("agent",)),
            entry("on.spatial", 3, "on", "relation", Question.WHERE),
            entry("the.definite", 5, "the", "property", Question.WHAT),
            entry(
                "bank.river-edge",
                7,
                "bank",
                "entity",
                Question.WHERE,
                valid_contexts=(("domain", "river"),),
            ),
            entry(
                "bank.financial-institution",
                11,
                "bank",
                "entity",
                Question.WHERE,
                valid_contexts=(("domain", "finance"),),
            ),
        )
    )


def test_lossless_unicode_forms_preserve_codepoint_and_byte_spans() -> None:
    sequence = LosslessFormEncoder().encode("Café sat—there.", PROFILE)

    assert sequence.original_utf8.decode("utf-8") == sequence.surface
    assert [form.surface for form in sequence.forms] == [
        "Café",
        "sat",
        "—",
        "there",
        ".",
    ]
    cafe = sequence.forms[0]
    assert cafe.codepoint_span == (0, 4)
    assert cafe.byte_span == (0, 5)
    assert sequence.original_utf8[slice(*cafe.byte_span)].decode("utf-8") == "Café"


def test_bank_candidates_remain_open_until_context_materially_selects_a_set() -> None:
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    dictionary = reference_dictionary()

    unresolved = dictionary.candidates(forms, ContextGraph())
    bank = unresolved[-1]
    assert {candidate.entry.sense for candidate in bank.viable} == {
        "bank.river-edge",
        "bank.financial-institution",
    }
    assert all("context-unresolved:domain" in candidate.reasons for candidate in bank.viable)

    river = dictionary.candidates(
        forms,
        ContextGraph.from_dict({"actor": "alice", "domain": "river"}),
    )[-1]
    assert [candidate.entry.sense for candidate in river.viable] == ["bank.river-edge"]
    assert river.candidates[0].status.value == "excluded"
    assert river.candidates[1].status.value == "viable"


def test_all_eleven_questions_are_represented_and_open_ideon_asks() -> None:
    """All eleven questions appear in InquiryState with evidence-driven deterministic
    behaviour.  WHO is material-open when a viable candidate carries a who-typed role
    and no actor is declared.  WHERE is contested when multiple location senses remain.
    WHY, HOW, WOULD are non-material open when a predicate is present; COULD is
    non-material open only when predicate and actor are both present."""
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    candidates = reference_dictionary().candidates(forms, ContextGraph())
    inquiry = InquiryEngine().query(forms, candidates, ContextGraph())

    assert tuple(result.question for result in inquiry.results) == QUESTIONS
    assert inquiry.get(Question.WHO).status is InquiryStatus.OPEN
    assert inquiry.get(Question.WHERE).status is InquiryStatus.CONTESTED
    assert inquiry.get(Question.WHERE).candidate_keys == (11, 7)
    decision = DecisionEngine().decide(inquiry)
    assert decision.decision is Decision.ASK
    assert decision.question is Question.WHO
    assert decision.evidence == ("who:actor", "where:location-sense")


def test_actor_and_river_context_close_only_the_required_openings() -> None:
    context = ContextGraph.from_dict({"actor": "alice", "domain": "river"})
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    candidates = reference_dictionary().candidates(forms, context)
    inquiry = InquiryEngine().query(forms, candidates, context)

    assert inquiry.get(Question.WHO).status is InquiryStatus.SATISFIED
    assert inquiry.get(Question.WHERE).status is InquiryStatus.SATISFIED
    assert inquiry.material_openings == ()
    assert DecisionEngine().decide(inquiry).decision is Decision.ANSWER


def test_unknown_dictionary_form_does_not_receive_an_invented_sense() -> None:
    forms = LosslessFormEncoder().encode("sat quux", PROFILE)
    candidates = reference_dictionary().candidates(forms, ContextGraph())
    inquiry = InquiryEngine().query(forms, candidates, ContextGraph.from_dict({"actor": "a"}))

    assert candidates[-1].candidates == ()
    assert inquiry.get(Question.WHAT).status is InquiryStatus.OPEN
    assert inquiry.get(Question.WHAT).material is True
    assert DecisionEngine().decide(inquiry).decision is Decision.ASK


def test_who_is_not_applicable_when_utterance_has_no_predicate() -> None:
    """WHO must not be materially open for every utterance lacking context.actor.
    When no action/state/relation is present nothing requires an agent."""
    forms = LosslessFormEncoder().encode("the bank", PROFILE)
    candidates = reference_dictionary().candidates(forms, ContextGraph())
    inquiry = InquiryEngine().query(forms, candidates, ContextGraph())

    # "the" is property, "bank" is entity — no predicate present.
    assert inquiry.get(Question.IS).status is InquiryStatus.OPEN
    assert inquiry.get(Question.IS).material is True
    assert inquiry.get(Question.WHO).status is InquiryStatus.NOT_APPLICABLE
    assert inquiry.get(Question.WHO).material is False


def test_should_high_expected_harm_stops_or_explicitly_offers_safer_route() -> None:
    context = ContextGraph.from_dict({"actor": "operator"})
    forms = LosslessFormEncoder().encode("sat", PROFILE)
    candidates = reference_dictionary().candidates(forms, context)
    base = dict(
        action="perform irreversible operation",
        affected_entities=("person",),
        norm_or_goal="avoid non-consensual harm",
        evidence=("declared-risk",),
        relevance=Decimal("0.9"),
        severity=Decimal("0.9"),
        confidence_or_margin=Decimal("0.9"),
        reversibility=Decimal("0.1"),
        context=context,
    )
    stop_assessment = ShouldAssessment(**base)
    inquiry = InquiryEngine().query(forms, candidates, context, should=stop_assessment)

    stopped = DecisionEngine().decide(inquiry, should=stop_assessment, action_requested=True)
    assert stopped.decision is Decision.STOP
    assert stopped.question is Question.SHOULD
    assert stopped.policy == (("should-stop-threshold", "0.64"),)

    safer_assessment = ShouldAssessment(
        **base,
        recommended_alternative="use the reversible dry-run",
    )
    safer = DecisionEngine().decide(inquiry, should=safer_assessment, action_requested=True)
    assert safer.decision is Decision.ASK
    assert safer.safer_alternative == "use the reversible dry-run"

    lower_threshold = DecisionEngine(Decimal("0.20")).decide(
        inquiry,
        should=replace(stop_assessment, severity=Decimal("0.3")),
        action_requested=True,
    )
    assert lower_threshold.policy == (("should-stop-threshold", "0.20"),)


def test_render_tense_classifies_event_as_was_is_or_projected() -> None:
    """render_tense compares WhenCondition instances; future tense is PROJECTED.
    A TIMESTAMP WhenCondition requires a timezone-aware instant."""
    event_time = datetime(2026, 7, 22, 12, tzinfo=UTC)
    event = WhenCondition(WhenKind.TIMESTAMP, instant=event_time)

    assert (
        render_tense(
            event,
            WhenCondition(WhenKind.TIMESTAMP, instant=event_time + timedelta(hours=1)),
        )
        is Tense.WAS
    )
    assert render_tense(event, WhenCondition(WhenKind.TIMESTAMP, instant=event_time)) is Tense.IS
    assert (
        render_tense(
            event,
            WhenCondition(WhenKind.TIMESTAMP, instant=event_time - timedelta(hours=1)),
        )
        is Tense.PROJECTED
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        WhenCondition(WhenKind.TIMESTAMP, instant=datetime(2026, 7, 22, 12))


def test_temporal_qualification_attaches_to_proposition_not_as_separate_inquiry() -> None:
    """Past/present/projected qualification wraps a WhenCondition and attaches to a
    proposition.  Equivalent propositions differ only in their TemporalQualification."""
    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    ref = WhenCondition(WhenKind.TIMESTAMP, instant=anchor)

    past_event = WhenCondition(WhenKind.TIMESTAMP, instant=anchor - timedelta(days=365))
    present_event = WhenCondition(WhenKind.TIMESTAMP, instant=anchor)
    future_event = WhenCondition(WhenKind.TIMESTAMP, instant=anchor + timedelta(days=365))

    past_q = TemporalQualification(render_tense(past_event, ref), past_event)
    present_q = TemporalQualification(render_tense(present_event, ref), present_event)
    future_q = TemporalQualification(render_tense(future_event, ref), future_event)

    assert past_q.tense is Tense.WAS
    assert present_q.tense is Tense.IS
    assert future_q.tense is Tense.PROJECTED
    assert past_q != present_q != future_q


def test_when_condition_satisfied_by_criterion_without_timestamp() -> None:
    """WHEN is condition-bearing: a CRITERION WhenCondition satisfies render_tense
    without any datetime when expressions match and status is 'satisfied'.
    WHEN is not reducible to a scalar datetime field."""
    criterion = "task-complete"
    event_cond = WhenCondition(WhenKind.CRITERION, expression=criterion, status="satisfied")
    reference_cond = WhenCondition(WhenKind.CRITERION, expression=criterion, status="satisfied")

    assert render_tense(event_cond, reference_cond) is Tense.IS


def test_whern_is_composite_of_where_and_when_components() -> None:
    """WHERN(P) is satisfied only when both the spatial/domain component
    (context.where or satisfied location candidates) and the temporal/conditional
    component (context.when in ``kind:expression`` format or context.time) are present.

    context.when must follow the bounded ``kind:expression`` format to satisfy WHEN;
    arbitrary strings fail closed.  A contested WHERE propagates into WHERN as
    CONTESTED when WHEN is satisfied; it propagates into OPEN with 'where:contested'
    in evidence when WHEN is missing.
    """
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    dictionary = reference_dictionary()

    # Both WHERE (context.where) and WHEN (criterion format) provided: WHERN SATISFIED
    ctx_both = ContextGraph.from_dict({"when": "criterion:task-complete", "where": "london"})
    inquiry_both = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_both), ctx_both
    )
    assert inquiry_both.get(Question.WHEN).status is InquiryStatus.SATISFIED
    assert inquiry_both.get(Question.WHERN).status is InquiryStatus.SATISFIED

    # Arbitrary string for context.when fails closed: WHEN NOT_APPLICABLE
    ctx_plain = ContextGraph.from_dict({"when": "task-complete", "where": "london"})
    inquiry_plain = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_plain), ctx_plain
    )
    assert inquiry_plain.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE

    # Only WHEN (criterion) provided with contested location candidates:
    # WHERE is CONTESTED (bank ambiguous); WHERN CONTESTED (propagates from WHERE)
    ctx_when_only = ContextGraph.from_dict({"when": "criterion:task-complete"})
    inquiry_when_only = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_when_only), ctx_when_only
    )
    assert inquiry_when_only.get(Question.WHERE).status is InquiryStatus.CONTESTED
    assert inquiry_when_only.get(Question.WHEN).status is InquiryStatus.SATISFIED
    assert inquiry_when_only.get(Question.WHERN).status is InquiryStatus.CONTESTED
    assert inquiry_when_only.get(Question.WHERN).material is True
    assert "where:contested" in inquiry_when_only.get(Question.WHERN).evidence

    # Only WHEN provided with no location candidates: WHERN OPEN (non-material), missing:where
    forms_no_loc = LosslessFormEncoder().encode("sat", PROFILE)
    inquiry_no_loc = InquiryEngine().query(
        forms_no_loc,
        dictionary.candidates(forms_no_loc, ctx_when_only),
        ctx_when_only,
    )
    assert inquiry_no_loc.get(Question.WHERE).status is InquiryStatus.NOT_APPLICABLE
    assert inquiry_no_loc.get(Question.WHERN).status is InquiryStatus.OPEN
    assert inquiry_no_loc.get(Question.WHERN).material is False
    assert "missing:where" in inquiry_no_loc.get(Question.WHERN).evidence

    # Only WHERE provided (context.where, no WHEN): WHERN OPEN (non-material), missing:when
    ctx_where_only = ContextGraph.from_dict({"where": "london"})
    inquiry_where_only = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_where_only), ctx_where_only
    )
    assert inquiry_where_only.get(Question.WHERN).status is InquiryStatus.OPEN
    assert inquiry_where_only.get(Question.WHERN).material is False
    assert "missing:when" in inquiry_where_only.get(Question.WHERN).evidence

    # Neither WHERE context nor WHEN provided, no location candidates: WHERN NOT_APPLICABLE
    forms_no_loc2 = LosslessFormEncoder().encode("sat", PROFILE)
    inquiry_neither = InquiryEngine().query(
        forms_no_loc2, dictionary.candidates(forms_no_loc2, ContextGraph()), ContextGraph()
    )
    assert inquiry_neither.get(Question.WHERE).status is InquiryStatus.NOT_APPLICABLE
    assert inquiry_neither.get(Question.WHERN).status is InquiryStatus.NOT_APPLICABLE


def test_whern_satisfied_by_dict_where_plus_context_time() -> None:
    """WHERN is also satisfied when the WHERE inquiry resolves via dictionary candidates
    (not only context.where) combined with a timestamp in context.time."""
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    dictionary = reference_dictionary()

    ctx = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-01-01T00:00:00Z"}
    )
    inquiry = InquiryEngine().query(forms, dictionary.candidates(forms, ctx), ctx)

    assert inquiry.get(Question.WHERE).status is InquiryStatus.SATISFIED
    assert inquiry.get(Question.WHEN).status is InquiryStatus.SATISFIED
    assert inquiry.get(Question.WHERN).status is InquiryStatus.SATISFIED


def test_who_not_applicable_for_impersonal_state_predicate() -> None:
    """WHO must be NOT_APPLICABLE when a predicate is present but carries no who-typed
    roles.  An impersonal state (semantic_roles=()) does not require an agent."""
    impersonal_state = entry(
        "rain.intransitive", 13, "rains", "state", Question.IS,
        semantic_roles=(),
    )
    dictionary = LogicDictionary((impersonal_state,))
    forms = LosslessFormEncoder().encode("rains", PROFILE)
    candidates = dictionary.candidates(forms, ContextGraph())
    inquiry = InquiryEngine().query(forms, candidates, ContextGraph())

    assert inquiry.get(Question.IS).status is InquiryStatus.SATISFIED
    assert inquiry.get(Question.WHO).status is InquiryStatus.NOT_APPLICABLE
    assert inquiry.get(Question.WHO).material is False


def test_who_open_for_agentive_predicate_without_actor() -> None:
    """WHO must be materially open when a viable candidate carries an agent role and no
    actor is declared in context.  Providing the actor resolves WHO to SATISFIED."""
    agentive = entry(
        "write.present", 13, "writes", "action", Question.WHAT, semantic_roles=("agent",)
    )
    dictionary = LogicDictionary((agentive,))
    forms = LosslessFormEncoder().encode("writes", PROFILE)
    candidates = dictionary.candidates(forms, ContextGraph())

    inquiry_no_actor = InquiryEngine().query(forms, candidates, ContextGraph())
    assert inquiry_no_actor.get(Question.WHO).status is InquiryStatus.OPEN
    assert inquiry_no_actor.get(Question.WHO).material is True

    ctx_with_actor = ContextGraph.from_dict({"actor": "alice"})
    inquiry_with_actor = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_with_actor), ctx_with_actor
    )
    assert inquiry_with_actor.get(Question.WHO).status is InquiryStatus.SATISFIED


def test_why_how_could_would_are_open_non_material_when_predicate_present() -> None:
    """WHY, HOW, WOULD are non-material open when a predicate is present.  COULD
    additionally requires a declared actor.  None of them block closure."""
    forms_with_pred = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    ctx_with_actor = ContextGraph.from_dict({"actor": "alice", "domain": "river"})
    candidates = reference_dictionary().candidates(forms_with_pred, ctx_with_actor)
    inquiry = InquiryEngine().query(forms_with_pred, candidates, ctx_with_actor)

    # All four are OPEN but non-material → do not block closure
    for q in (Question.WHY, Question.HOW, Question.WOULD):
        assert inquiry.get(q).status is InquiryStatus.OPEN, q
        assert inquiry.get(q).material is False, q
    assert inquiry.get(Question.COULD).status is InquiryStatus.OPEN
    assert inquiry.get(Question.COULD).material is False

    assert inquiry.material_openings == ()
    assert DecisionEngine().decide(inquiry).decision is Decision.ANSWER


def test_could_not_applicable_when_no_actor_declared() -> None:
    """COULD requires both a predicate and a declared actor.  Without an actor,
    capability assessment is not applicable."""
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    candidates = reference_dictionary().candidates(forms, ContextGraph())
    inquiry = InquiryEngine().query(forms, candidates, ContextGraph())

    assert inquiry.get(Question.COULD).status is InquiryStatus.NOT_APPLICABLE


def test_temporal_and_volitional_auxiliary_relations_are_classified_separately() -> None:
    """The resolved relation (not the surface token) determines the auxiliary kind."""
    assert auxiliary_kind("future-state") is AuxiliaryKind.TEMPORAL
    assert auxiliary_kind("intends") is AuxiliaryKind.VOLITIONAL
    with pytest.raises(ValueError, match="unresolved auxiliary relation"):
        auxiliary_kind("will")


def test_invalid_time_string_does_not_satisfy_when() -> None:
    """context.time must be a parseable, timezone-aware ISO timestamp.
    An invalid or timezone-naive string must fail closed: WHEN stays NOT_APPLICABLE."""
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    dictionary = reference_dictionary()

    # Invalid timestamp: non-parseable string → WHEN NOT_APPLICABLE
    ctx_invalid = ContextGraph.from_dict({"time": "not-a-date"})
    inquiry_invalid = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_invalid), ctx_invalid
    )
    assert inquiry_invalid.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE

    # Timezone-naive datetime string → WHEN NOT_APPLICABLE (fail closed)
    ctx_naive = ContextGraph.from_dict({"time": "2026-01-01T00:00:00"})
    inquiry_naive = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_naive), ctx_naive
    )
    assert inquiry_naive.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE

    # Valid timezone-aware ISO timestamp → WHEN SATISFIED
    ctx_valid = ContextGraph.from_dict({"time": "2026-01-01T00:00:00Z"})
    inquiry_valid = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_valid), ctx_valid
    )
    assert inquiry_valid.get(Question.WHEN).status is InquiryStatus.SATISFIED


def test_whern_constituent_state_propagates_contested_where() -> None:
    """WHERN must propagate CONTESTED from a contested WHERE constituent.
    A contested WHERE plus a satisfied WHEN yields WHERN CONTESTED (material=True),
    not OPEN.  WHERN can only be SATISFIED when WHERE is fully resolved (not contested).
    A contested WHERE with no WHEN yields WHERN OPEN (missing:when).
    This is the complete WHERE x WHEN state matrix for the contested-WHERE column."""
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    dictionary = reference_dictionary()

    # WHERE CONTESTED (bank ambiguous) + WHEN SATISFIED → WHERN CONTESTED (material)
    ctx = ContextGraph.from_dict({"time": "2026-01-01T00:00:00Z"})
    inquiry = InquiryEngine().query(forms, dictionary.candidates(forms, ctx), ctx)

    assert inquiry.get(Question.WHERE).status is InquiryStatus.CONTESTED
    assert inquiry.get(Question.WHEN).status is InquiryStatus.SATISFIED
    assert inquiry.get(Question.WHERN).status is InquiryStatus.CONTESTED
    assert inquiry.get(Question.WHERN).material is True
    assert "where:contested" in inquiry.get(Question.WHERN).evidence

    # Resolving WHERE (domain=river) + WHEN SATISFIED → WHERN SATISFIED
    ctx_resolved = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-01-01T00:00:00Z"}
    )
    inquiry_resolved = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_resolved), ctx_resolved
    )
    assert inquiry_resolved.get(Question.WHERE).status is InquiryStatus.SATISFIED
    assert inquiry_resolved.get(Question.WHERN).status is InquiryStatus.SATISFIED

    # WHERE CONTESTED + WHEN NOT_APPLICABLE → WHERN OPEN (non-material, missing:when)
    ctx_no_when = ContextGraph.from_dict({})
    inquiry_no_when = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_no_when), ctx_no_when
    )
    assert inquiry_no_when.get(Question.WHERE).status is InquiryStatus.CONTESTED
    assert inquiry_no_when.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE
    assert inquiry_no_when.get(Question.WHERN).status is InquiryStatus.OPEN
    assert inquiry_no_when.get(Question.WHERN).material is False
    assert "missing:when" in inquiry_no_when.get(Question.WHERN).evidence


def test_whern_complete_state_matrix() -> None:
    """Complete WHERE x WHEN state matrix for WHERN.

    SATISFIED only when WHERE fully satisfied and WHEN satisfied.
    CONTESTED when WHERE contested and WHEN satisfied.
    OPEN (non-material) when WHERE present/contested + WHEN missing, or WHEN + no WHERE.
    NOT_APPLICABLE when neither WHERE nor WHEN is present.
    """
    forms = LosslessFormEncoder().encode("sat on the bank", PROFILE)
    forms_no_loc = LosslessFormEncoder().encode("sat", PROFILE)
    dictionary = reference_dictionary()

    # WHERE SATISFIED + WHEN SATISFIED → WHERN SATISFIED
    ctx_sat_sat = ContextGraph.from_dict(
        {"actor": "alice", "domain": "river", "time": "2026-01-01T00:00:00Z"}
    )
    inq_sat_sat = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_sat_sat), ctx_sat_sat
    )
    assert inq_sat_sat.get(Question.WHERE).status is InquiryStatus.SATISFIED
    assert inq_sat_sat.get(Question.WHEN).status is InquiryStatus.SATISFIED
    assert inq_sat_sat.get(Question.WHERN).status is InquiryStatus.SATISFIED

    # WHERE CONTESTED + WHEN SATISFIED → WHERN CONTESTED
    ctx_con_sat = ContextGraph.from_dict({"time": "2026-01-01T00:00:00Z"})
    inq_con_sat = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_con_sat), ctx_con_sat
    )
    assert inq_con_sat.get(Question.WHERE).status is InquiryStatus.CONTESTED
    assert inq_con_sat.get(Question.WHEN).status is InquiryStatus.SATISFIED
    assert inq_con_sat.get(Question.WHERN).status is InquiryStatus.CONTESTED
    assert inq_con_sat.get(Question.WHERN).material is True

    # WHERE SATISFIED + WHEN NOT_APPLICABLE → WHERN OPEN (missing:when)
    ctx_sat_na = ContextGraph.from_dict({"actor": "alice", "domain": "river"})
    inq_sat_na = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_sat_na), ctx_sat_na
    )
    assert inq_sat_na.get(Question.WHERE).status is InquiryStatus.SATISFIED
    assert inq_sat_na.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE
    assert inq_sat_na.get(Question.WHERN).status is InquiryStatus.OPEN
    assert inq_sat_na.get(Question.WHERN).material is False
    assert "missing:when" in inq_sat_na.get(Question.WHERN).evidence

    # WHERE CONTESTED + WHEN NOT_APPLICABLE → WHERN OPEN (where:contested, missing:when)
    ctx_con_na = ContextGraph.from_dict({})
    inq_con_na = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_con_na), ctx_con_na
    )
    assert inq_con_na.get(Question.WHERE).status is InquiryStatus.CONTESTED
    assert inq_con_na.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE
    assert inq_con_na.get(Question.WHERN).status is InquiryStatus.OPEN
    assert inq_con_na.get(Question.WHERN).material is False
    assert "where:contested" in inq_con_na.get(Question.WHERN).evidence
    assert "missing:when" in inq_con_na.get(Question.WHERN).evidence

    # WHERE NOT_APPLICABLE + WHEN SATISFIED → WHERN OPEN (missing:where)
    ctx_na_sat = ContextGraph.from_dict({"time": "2026-01-01T00:00:00Z"})
    inq_na_sat = InquiryEngine().query(
        forms_no_loc,
        dictionary.candidates(forms_no_loc, ctx_na_sat),
        ctx_na_sat,
    )
    assert inq_na_sat.get(Question.WHERE).status is InquiryStatus.NOT_APPLICABLE
    assert inq_na_sat.get(Question.WHEN).status is InquiryStatus.SATISFIED
    assert inq_na_sat.get(Question.WHERN).status is InquiryStatus.OPEN
    assert inq_na_sat.get(Question.WHERN).material is False
    assert "missing:where" in inq_na_sat.get(Question.WHERN).evidence

    # WHERE NOT_APPLICABLE + WHEN NOT_APPLICABLE → WHERN NOT_APPLICABLE
    inq_na_na = InquiryEngine().query(
        forms_no_loc,
        dictionary.candidates(forms_no_loc, ContextGraph()),
        ContextGraph(),
    )
    assert inq_na_na.get(Question.WHERE).status is InquiryStatus.NOT_APPLICABLE
    assert inq_na_na.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE
    assert inq_na_na.get(Question.WHERN).status is InquiryStatus.NOT_APPLICABLE


def test_who_satisfied_by_non_agent_role() -> None:
    """WHO must be satisfied by the context field that corresponds to the candidate's
    actual who-typed role, not exclusively by context.actor (agent).
    An experiencer role is satisfied by context.experiencer; the emitted role name
    is the actual context field, not always 'actor'."""
    experiencer_entry = entry(
        "feel.state", 17, "feels", "state", Question.IS,
        semantic_roles=("experiencer",),
    )
    dictionary = LogicDictionary((experiencer_entry,))
    forms = LosslessFormEncoder().encode("feels", PROFILE)

    # No who-typed context: WHO OPEN with role "experiencer"
    candidates_open = dictionary.candidates(forms, ContextGraph())
    inquiry_open = InquiryEngine().query(forms, candidates_open, ContextGraph())
    assert inquiry_open.get(Question.WHO).status is InquiryStatus.OPEN
    assert inquiry_open.get(Question.WHO).role == "experiencer"
    assert inquiry_open.get(Question.WHO).material is True

    # context.actor alone does NOT satisfy an experiencer role
    ctx_actor = ContextGraph.from_dict({"actor": "alice"})
    inquiry_actor = InquiryEngine().query(forms, dictionary.candidates(forms, ctx_actor), ctx_actor)
    assert inquiry_actor.get(Question.WHO).status is InquiryStatus.OPEN

    # context.experiencer satisfies the experiencer role: WHO SATISFIED, role="experiencer"
    ctx_experiencer = ContextGraph.from_dict({"experiencer": "alice"})
    inquiry_sat = InquiryEngine().query(
        forms, dictionary.candidates(forms, ctx_experiencer), ctx_experiencer
    )
    assert inquiry_sat.get(Question.WHO).status is InquiryStatus.SATISFIED
    assert inquiry_sat.get(Question.WHO).role == "experiencer"


def test_parse_when_expression_valid_and_fail_closed() -> None:
    """_parse_when_expression validates bounded kind:expression format.
    Valid forms return a WhenCondition; arbitrary strings fail closed (return None)."""
    from af.domain.ideons.inquiry import _parse_when_expression

    # Event kind
    cond = _parse_when_expression("event:door-opened")
    assert cond is not None
    assert cond.kind is WhenKind.EVENT
    assert cond.expression == "door-opened"

    # Criterion kind
    cond = _parse_when_expression("criterion:task-complete")
    assert cond is not None
    assert cond.kind is WhenKind.CRITERION
    assert cond.expression == "task-complete"

    # Interval kind (half-open: only start)
    cond = _parse_when_expression("interval:2026-01-01T00:00:00Z,")
    assert cond is not None
    assert cond.kind is WhenKind.INTERVAL
    assert cond.interval_start is not None
    assert cond.interval_end is None

    # Arbitrary string — fail closed
    assert _parse_when_expression("task-complete") is None
    assert _parse_when_expression("") is None
    assert _parse_when_expression("unknown:value") is None
    # Timezone-naive interval — fail closed
    assert _parse_when_expression("interval:2026-01-01T00:00:00,") is None
    # Empty expression — fail closed
    assert _parse_when_expression("event:") is None
    assert _parse_when_expression("criterion:") is None
    # Fully open interval (both ends empty) — fail closed
    assert _parse_when_expression("interval:,") is None


def test_who_remains_open_when_only_some_required_roles_satisfied() -> None:
    """WHO must not close after the first matching role.
    When a predicate declares multiple who-typed roles (e.g. agent + affected),
    supplying only one context field leaves WHO open until all required roles are
    present.  Evidence names every missing role."""
    multi_role_entry = entry(
        "give.transitive",
        17,
        "gives",
        "action",
        Question.WHAT,
        semantic_roles=("agent", "affected"),
    )
    dictionary = LogicDictionary((multi_role_entry,))
    forms = LosslessFormEncoder().encode("gives", PROFILE)

    # Only actor supplied (satisfies agent) but affected is still missing → WHO OPEN
    ctx_agent_only = ContextGraph.from_dict({"actor": "alice"})
    candidates_agent = dictionary.candidates(forms, ctx_agent_only)
    inquiry_agent = InquiryEngine().query(forms, candidates_agent, ctx_agent_only)

    assert inquiry_agent.get(Question.WHO).status is InquiryStatus.OPEN
    assert inquiry_agent.get(Question.WHO).material is True
    assert "missing:affected" in inquiry_agent.get(Question.WHO).evidence

    # Both actor and affected supplied → WHO SATISFIED
    ctx_both = ContextGraph.from_dict({"actor": "alice", "affected": "book"})
    candidates_both = dictionary.candidates(forms, ctx_both)
    inquiry_both = InquiryEngine().query(forms, candidates_both, ctx_both)

    assert inquiry_both.get(Question.WHO).status is InquiryStatus.SATISFIED
    assert inquiry_both.get(Question.WHO).material is False

    # Neither supplied → WHO OPEN (all roles missing)
    candidates_none = dictionary.candidates(forms, ContextGraph())
    inquiry_none = InquiryEngine().query(forms, candidates_none, ContextGraph())

    assert inquiry_none.get(Question.WHO).status is InquiryStatus.OPEN
    assert inquiry_none.get(Question.WHO).material is True
    assert "missing:actor" in inquiry_none.get(Question.WHO).evidence
    assert "missing:affected" in inquiry_none.get(Question.WHO).evidence


def test_when_satisfied_by_event_expression_without_timestamp() -> None:
    """WHEN is satisfied by a valid event:expression without any datetime value.
    WHEN is also satisfied by a criterion:expression.
    Arbitrary strings without a kind prefix fail closed."""
    forms = LosslessFormEncoder().encode("sat", PROFILE)
    dictionary = reference_dictionary()

    # event: prefix → WHEN SATISFIED
    ctx_event = ContextGraph.from_dict({"when": "event:door-opened"})
    inq_event = InquiryEngine().query(forms, dictionary.candidates(forms, ctx_event), ctx_event)
    assert inq_event.get(Question.WHEN).status is InquiryStatus.SATISFIED

    # criterion: prefix → WHEN SATISFIED
    ctx_crit = ContextGraph.from_dict({"when": "criterion:approval-granted"})
    inq_crit = InquiryEngine().query(forms, dictionary.candidates(forms, ctx_crit), ctx_crit)
    assert inq_crit.get(Question.WHEN).status is InquiryStatus.SATISFIED

    # Arbitrary string without prefix → WHEN NOT_APPLICABLE (fail closed)
    ctx_plain = ContextGraph.from_dict({"when": "approval-granted"})
    inq_plain = InquiryEngine().query(forms, dictionary.candidates(forms, ctx_plain), ctx_plain)
    assert inq_plain.get(Question.WHEN).status is InquiryStatus.NOT_APPLICABLE
