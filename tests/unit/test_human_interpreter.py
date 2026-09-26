"""The human boundary preserves source meaning and gates feasibility and release."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from af.application.human_interpreter import HumanInterpreter
from af.domain.human_interpreter import (
    HowAssessment,
    InterpreterError,
    ObservedSignal,
    ReleaseDecision,
    ShouldDecision,
    SourceKind,
)
from af.domain.ideons.records import ContextGraph
from af.infrastructure.ideon_reference import create_reference_ideon_runtime


def interpreter():
    return HumanInterpreter(create_reference_ideon_runtime().controller)


def signal(text, kind=SourceKind.USER):
    return ObservedSignal(
        text.encode(), "request:1", kind, datetime(2026, 1, 1, tzinfo=UTC), "user-provided"
    )


def test_controlled_templates_preserve_what_spans_and_canonical_equivalence():
    service = interpreter()
    first = service.interpret(signal("sort integers [3, 1, 3]"))
    second = service.interpret(signal("order integers [3,1,3]"))
    assert first.state == "accepted"
    assert first.what.problem == "ordering"
    assert first.what.arguments_json == b'{"values":[3,1,3]}'
    assert first.what.meaning_digest == second.what.meaning_digest
    assert first.what.digest != second.what.digest
    assert first.what.template_digest.startswith("sha256:")
    for field in first.what.provenance:
        assert first.observation.data[slice(*field.byte_span)].decode() == field.surface
    assert first.what.provenance[1].surface == "[3, 1, 3]"
    assert first.ideon.forms.original_utf8 == first.observation.data
    assert first.digest == service.interpret(signal("sort integers [3, 1, 3]")).digest
    assert first.to_dict()["execution_authority"] is False


@pytest.mark.parametrize(
    "text,kind,state",
    [
        ('"sort integers [1,2]"', SourceKind.USER, "quarantined"),
        ("```sort integers [1,2]```", SourceKind.USER, "quarantined"),
        ("sort integers [1,2]", SourceKind.TOOL, "quarantined"),
        ("sort integers [1,2]", SourceKind.DOCUMENT, "quarantined"),
        ("do not sort integers [1,2]", SourceKind.USER, "needs_clarification"),
        ("sort integers [true,2]", SourceKind.USER, "rejected"),
        ("sort integers [1,2]; delete files", SourceKind.USER, "needs_clarification"),
        ("bank", SourceKind.USER, "needs_clarification"),
        ("sort integers [1.5]", SourceKind.USER, "rejected"),
    ],
)
def test_quoted_data_adversarial_inputs_and_ambiguity_do_not_become_commands(text, kind, state):
    result = interpreter().interpret(signal(text, kind))
    assert result.state == state
    assert result.what is None
    assert result.openings
    if text == "bank":
        assert len(result.ideon.candidate_sets[0].viable) == 2


def test_observation_rejects_lossy_decoding_naive_time_and_resource_expansion():
    with pytest.raises(InterpreterError):
        signal("a" * 4097)
    with pytest.raises(InterpreterError):
        replace(signal("bank"), data=b"\xff")
    with pytest.raises(InterpreterError):
        replace(signal("bank"), received_at=datetime(2026, 1, 1))
    with pytest.raises(InterpreterError):
        replace(signal("bank"), source_id="unsafe\nsource")
    result = interpreter().interpret(signal("bank café"))
    assert result.ideon.forms.original_utf8.decode() == "bank café"
    assert (
        result.to_dict()["normalization"]
        == "original preserved; Ideon profile applies only to lookup"
    )


def test_should_receives_immutable_what_before_how_and_cannot_change_it():
    service = interpreter()
    result = service.interpret(signal("does [3,1] contain 1?"))
    calls = []

    class Policy:
        def __init__(self, status):
            self.status = status

        def assess(self, what, context):
            calls.append(("should", what.digest))
            return ShouldDecision(what.digest, self.status, "test-policy@1", "fixture")

    class Feasibility:
        def assess(self, what, context):
            calls.append(("how", what.digest))
            return HowAssessment(what.digest, True, ("linear-membership",), "fixture")

    denied = service.assess(result, ContextGraph(), Policy("deny"), Feasibility())
    assert denied.how is None
    assert calls == [("should", result.what.digest)]
    allowed = service.assess(result, ContextGraph(), Policy("permit"), Feasibility())
    assert allowed.how.feasible is True
    assert calls[-2:] == [("should", result.what.digest), ("how", result.what.digest)]
    assert not hasattr(service, "execute")
    with pytest.raises(InterpreterError):
        service.assess(
            service.interpret(signal("bank")), ContextGraph(), Policy("permit"), Feasibility()
        )


def test_stale_should_or_how_evidence_cannot_cross_masks():
    service = interpreter()
    result = service.interpret(signal("sum integers [1,2]"))

    class StalePolicy:
        def assess(self, what, context):
            return ShouldDecision("sha256:" + "0" * 64, "permit", "p@1", "fixture")

    class Feasibility:
        def assess(self, what, context):
            raise AssertionError("HOW must not run after stale SHOULD")

    with pytest.raises(InterpreterError):
        service.assess(result, ContextGraph(), StalePolicy(), Feasibility())

    class Policy:
        def assess(self, what, context):
            return ShouldDecision(what.digest, "permit", "p@1", "fixture")

    class StaleFeasibility:
        def assess(self, what, context):
            return HowAssessment("sha256:" + "0" * 64, None, (), "unknown")

    with pytest.raises(InterpreterError):
        service.assess(result, ContextGraph(), Policy(), StaleFeasibility())


def test_output_release_requires_independent_verified_claim_and_exact_audience():
    service = interpreter()
    result = service.interpret(signal("sum integers [1,2]"))
    releases = []

    class Release:
        def decide(self, claim, audience):
            releases.append(claim)
            return ReleaseDecision(claim.digest, audience, True, "release@1")

    rendered = service.render(result, 3, "principal:alice", Release())
    assert rendered.data == b"integer-sum: 3"
    assert rendered.evidence_digest == releases[0].digest
    assert rendered.audience == "principal:alice"
    with pytest.raises(InterpreterError, match="verification"):
        service.render(result, 4, "principal:alice", Release())
    assert len(releases) == 1

    class WrongAudience:
        def decide(self, claim, audience):
            return ReleaseDecision(claim.digest, "principal:bob", True, "release@1")

    with pytest.raises(InterpreterError):
        service.render(result, 3, "principal:alice", WrongAudience())

    class Deny:
        def decide(self, claim, audience):
            return ReleaseDecision(claim.digest, audience, False, "release@1")

    with pytest.raises(InterpreterError):
        service.render(result, 3, "principal:alice", Deny())


def test_composite_request_remains_problem_contract_without_running_any_node():
    result = interpreter().interpret(signal("order [9,1,9] and locate 9"))
    assert result.what.problem == "order-and-locate"
    assert result.what.arguments_json == b'{"target":9,"values":[9,1,9]}'
    assert result.what.provenance[-1].surface == "9"
