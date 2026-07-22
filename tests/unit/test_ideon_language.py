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
from af.domain.ideons.temporal import Tense, Whern, WillKind, render_tense, will_kind

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
    )


def reference_dictionary() -> LogicDictionary:
    return LogicDictionary(
        (
            entry("sit.past", 2, "sat", "action", Question.WHAT),
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


def test_all_eleven_inquiries_are_actual_output_and_open_ideon_asks() -> None:
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


def test_one_event_renders_as_was_is_or_will_by_reference_whern() -> None:
    event_time = datetime(2026, 7, 22, 12, tzinfo=UTC)
    event = Whern(event_time, None)

    assert render_tense(event, Whern(None, event_time + timedelta(hours=1))) is Tense.WAS
    assert render_tense(event, Whern(None, event_time)) is Tense.IS
    assert render_tense(event, Whern(None, event_time - timedelta(hours=1))) is Tense.WILL
    with pytest.raises(ValueError, match="timezone-aware"):
        Whern(datetime(2026, 7, 22, 12), None)


def test_temporal_will_and_volitional_will_are_distinct_resolved_relations() -> None:
    assert will_kind("future-state") is WillKind.TEMPORAL
    assert will_kind("intends") is WillKind.VOLITIONAL
    with pytest.raises(ValueError, match="unresolved will"):
        will_kind("will")
