"""Typed decisions preserve uncertainty and have no execution authority."""

from dataclasses import FrozenInstanceError
from typing import cast

import pytest

from af.framework.decisions import (
    ChoiceOption,
    DecisionAnswer,
    DecisionKind,
    DecisionRequest,
    DecisionStatus,
    DeterministicDecisionProvider,
    Probability,
    reduce_decision,
)


def request() -> DecisionRequest:
    return DecisionRequest(
        kind=DecisionKind.CHOICE,
        question="Which installed procedure fits?",
        state="count letters",
        options=(ChoiceOption("count", "Count a character"), ChoiceOption("none", "No match")),
    )


def test_decision_request_is_immutable_bounded_and_content_addressed() -> None:
    value = request()
    assert value.digest == request().digest
    with pytest.raises(FrozenInstanceError):
        value.state = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError):
        DecisionRequest(kind=DecisionKind.BINARY, question="?", state="x" * 32_001)
    with pytest.raises(ValueError):
        DecisionRequest(kind=DecisionKind.CHOICE, question="?", state="x", options=())
    with pytest.raises(ValueError):
        DecisionRequest(
            kind=DecisionKind.CHOICE, question="?", state="x", options=(ChoiceOption("x", "x"),) * 2
        )
    with pytest.raises(ValueError):
        DecisionRequest(
            kind=DecisionKind.BINARY, question="?", state="x", options=(ChoiceOption("x", "x"),)
        )


@pytest.mark.parametrize("value", [True, -0.1, 1.1, float("nan"), float("inf"), "0.9"])
def test_probabilities_are_strict_finite_numbers(value: object) -> None:
    with pytest.raises(ValueError):
        Probability("count", cast(float, value))


def test_choice_threshold_uses_probability_not_provider_concentration() -> None:
    answer = DecisionAnswer(
        request_digest=request().digest,
        provider="test",
        model="test-v1",
        probabilities=(Probability("count", 0.9), Probability("none", 0.1)),
        provider_confidence=0.4,
    )
    assert reduce_decision(request(), answer, threshold=0.8) == "count"
    assert reduce_decision(request(), answer, threshold=0.95) is None
    assert answer.provider_confidence == 0.4


def test_invalid_distributions_and_request_mismatch_never_reduce() -> None:
    for probabilities in [
        (Probability("unknown", 1),),
        (Probability("count", 0.5), Probability("none", 0.4)),
        (Probability("count", 0.5), Probability("count", 0.5)),
        (Probability("count", 0.5), Probability("none", 0.5)),
    ]:
        answer = DecisionAnswer(
            request_digest=request().digest,
            provider="test",
            model="v1",
            probabilities=probabilities,
        )
        assert reduce_decision(request(), answer, threshold=0.0) is None
    answer = DecisionAnswer(
        request_digest="sha256:" + "0" * 64,
        provider="test",
        model="v1",
        probabilities=(Probability("count", 1), Probability("none", 0)),
    )
    assert reduce_decision(request(), answer) is None


def test_binary_abstains_between_thresholds_and_provider_is_optional() -> None:
    req = DecisionRequest(kind=DecisionKind.BINARY, question="Is this understood?", state="text")
    provider = DeterministicDecisionProvider(
        lambda value: DecisionAnswer(
            request_digest=value.digest, provider="deterministic", model="rule-v1", probability=0.95
        )
    )
    assert reduce_decision(req, provider.evaluate(req)) is True
    ambiguous = DecisionAnswer(
        request_digest=req.digest, provider="test", model="v1", probability=0.5
    )
    assert reduce_decision(req, ambiguous) is None
    negative = DecisionAnswer(
        request_digest=req.digest, provider="test", model="v1", probability=0.05
    )
    assert reduce_decision(req, negative) is False
    assert provider.evaluate(req, cancelled=lambda: True).status is DecisionStatus.CANCELLED
    assert (
        reduce_decision(
            req,
            DecisionAnswer(
                request_digest=req.digest, provider="test", status=DecisionStatus.UNAVAILABLE
            ),
        )
        is None
    )
    with pytest.raises(ValueError):
        reduce_decision(req, ambiguous, threshold=0.4)
