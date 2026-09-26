"""Closed-set decisions select trusted, already bound procedure proposals."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

from af.framework.decisions import (
    ChoiceOption,
    DecisionKind,
    DecisionProvider,
    DecisionRequest,
    DecisionStatus,
    reduce_decision,
)
from af.framework.types import (
    ProcedureDescriptor,
    SelectionDecision,
    SelectionHandoffReason,
    SelectionProposal,
)

CandidateBuilder = Callable[[str, tuple[ProcedureDescriptor, ...]], tuple[SelectionProposal, ...]]


class DecisionProcedureSelector:
    """No argument generation: a trusted deterministic builder owns candidate values.

    The provider judges a closed set and cannot introduce a procedure or an
    argument. Returned proposals still require the pipeline's grounding, policy
    and independent verification. For remote decisions use a consent-aware
    provider configured by the application; this adapter grants no consent.
    """

    def __init__(
        self, provider: DecisionProvider, candidates: CandidateBuilder, *, threshold: float = 0.8
    ) -> None:
        from af.framework.types import parse_probability

        if parse_probability(threshold) is None:
            raise ValueError("invalid decision selector threshold")
        self.name = provider.name
        self.locality = provider.locality
        self._provider = provider
        self._candidates = candidates
        self._threshold = threshold

    def select(self, prompt: str, catalogue: tuple[ProcedureDescriptor, ...]) -> SelectionDecision:
        proposals = self._candidates(prompt, catalogue)
        if type(proposals) is not tuple or not 1 <= len(proposals) <= 254:
            return SelectionDecision(reason=SelectionHandoffReason.UNSUPPORTED)
        installed = {item.procedure_id: item for item in catalogue}
        bound: list[SelectionProposal] = []
        options: list[ChoiceOption] = [
            ChoiceOption("no-match", "No candidate matches, or request is ambiguous")
        ]
        for index, proposal in enumerate(proposals):
            item = installed.get(proposal.procedure_id)
            if (
                item is None
                or proposal.intent != item.intent
                or (proposal.version is not None and proposal.version != item.version)
                or (proposal.digest is not None and proposal.digest != item.digest)
            ):
                return SelectionDecision(reason=SelectionHandoffReason.MALFORMED)
            bound.append(replace(proposal, version=item.version, digest=item.digest))
            options.append(
                ChoiceOption(
                    f"candidate-{index}",
                    json.dumps(
                        {
                            "procedure": item.procedure_id,
                            "intent": item.intent,
                            "version": item.version,
                            "arguments": proposal.arguments,
                        },
                        sort_keys=True,
                    ),
                )
            )
        request = DecisionRequest(
            DecisionKind.CHOICE,
            "Which single candidate precisely satisfies the request? "
            "Choose no-match for ambiguity or missing information.",
            prompt,
            tuple(options),
        )
        result = self._provider.evaluate(request)
        selected = reduce_decision(request, result, threshold=self._threshold)
        if type(selected) is not str or selected == "no-match":
            reason = {
                DecisionStatus.CANCELLED: SelectionHandoffReason.CANCELLED,
                DecisionStatus.TIMEOUT: SelectionHandoffReason.TIMEOUT,
                DecisionStatus.MALFORMED: SelectionHandoffReason.MALFORMED,
                DecisionStatus.UNAVAILABLE: SelectionHandoffReason.UNAVAILABLE,
                DecisionStatus.DENIED: SelectionHandoffReason.UNAVAILABLE,
            }.get(result.status, SelectionHandoffReason.AMBIGUOUS)
            return SelectionDecision(reason=reason)
        index = int(selected.removeprefix("candidate-"))
        probability = next(item.value for item in result.probabilities if item.option == selected)
        return SelectionDecision(proposal=replace(bound[index], confidence=probability))
