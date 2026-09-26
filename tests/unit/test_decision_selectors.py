"""Typed decisions can select one existing bound procedure without generating arguments."""

from af.domain.procedures import CharacterCountProcedure
from af.framework.decision_selectors import DecisionProcedureSelector
from af.framework.decisions import (
    DecisionAnswer,
    DecisionStatus,
    DeterministicDecisionProvider,
    Probability,
)
from af.framework.routing import ProcedureRegistry
from af.framework.types import SelectionHandoffReason, SelectionProposal, SelectorLocality


def test_decision_selector_preserves_bound_arguments_and_winner_probability() -> None:
    registry = ProcedureRegistry()
    registry.register(CharacterCountProcedure())
    provider = DeterministicDecisionProvider(
        lambda request: DecisionAnswer(
            request.digest,
            "deterministic",
            model="rule-v1",
            probabilities=(Probability("no-match", 0.05), Probability("candidate-0", 0.95)),
            provider_confidence=0.3,
        )
    )
    selector = DecisionProcedureSelector(
        provider,
        lambda prompt, catalogue: (
            SelectionProposal(
                "character-count",
                "text.character_count",
                {"text": "strawberry", "character": "r"},
                0.1,
            ),
        ),
    )
    result = selector.select("please count r in strawberry", registry.catalogue())
    assert result.proposal is not None and result.proposal.confidence == 0.95
    assert result.proposal.arguments == {"text": "strawberry", "character": "r"}
    assert (
        result.proposal.version == "1.0.0"
        and result.proposal.digest == registry.catalogue()[0].digest
    )
    assert selector.locality is SelectorLocality.LOCAL


def test_decision_selector_unknown_candidate_abstention_and_failure_do_not_execute() -> None:
    registry = ProcedureRegistry()
    registry.register(CharacterCountProcedure())
    provider = DeterministicDecisionProvider(
        lambda request: DecisionAnswer(request.digest, "deterministic", DecisionStatus.TIMEOUT)
    )

    def candidates(prompt, catalogue):  # type: ignore[no-untyped-def]
        return (
            SelectionProposal(
                "character-count",
                "text.character_count",
                {"text": "strawberry", "character": "r"},
                1,
            ),
        )

    assert (
        DecisionProcedureSelector(provider, candidates).select("text", registry.catalogue()).reason
        is SelectionHandoffReason.TIMEOUT
    )
    assert (
        DecisionProcedureSelector(provider, candidates).select("text", ()).reason
        is SelectionHandoffReason.MALFORMED
    )
    assert (
        DecisionProcedureSelector(provider, lambda prompt, catalogue: ())
        .select("text", registry.catalogue())
        .reason
        is SelectionHandoffReason.UNSUPPORTED
    )
