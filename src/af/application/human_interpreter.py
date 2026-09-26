"""Bidirectional human boundary around the existing controlled Ideon runtime.

The interpreter produces source-bound meaning, feasibility records, and rendered
signals. It has no execution or delivery port and cannot grant either authority.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from af.application.ideons import IdeonController, InterpretRequest
from af.domain.algorithms.contracts import AlgorithmError, canonical, digest, parse_json
from af.domain.human_interpreter import (
    HowPort,
    HumanInterpretation,
    InterpreterError,
    MaskAssessment,
    ObservedSignal,
    OutboundClaim,
    ReleasePort,
    RenderedSignal,
    ShouldPort,
    SourceKind,
    SourceSpan,
    WhatContract,
)
from af.domain.ideons.records import ContextGraph
from af.domain.problems import ProblemCatalogue


@dataclass(frozen=True, slots=True)
class RequestTemplate:
    id: str
    problem: str
    expression: str
    slots: tuple[str, ...]
    version: str = "1.0.0"

    @property
    def digest(self) -> str:
        return digest(
            {
                "template": asdict(self),
                "problem_digest": ProblemCatalogue.descriptor(self.problem).digest,
            }
        )


TEMPLATES = (
    RequestTemplate(
        "af.en.order@1",
        "ordering",
        r"(?:sort|order) integers (?P<values>\[[^\[\]\n]*\])",
        ("values",),
    ),
    RequestTemplate(
        "af.en.sum@1", "integer-sum", r"sum integers (?P<values>\[[^\[\]\n]*\])", ("values",)
    ),
    RequestTemplate(
        "af.en.membership@1",
        "integer-membership",
        r"does (?P<values>\[[^\[\]\n]*\]) contain (?P<target>-?[0-9]{1,20})\?",
        ("values", "target"),
    ),
    RequestTemplate(
        "af.en.order-locate@1",
        "order-and-locate",
        r"order (?P<values>\[[^\[\]\n]*\]) and locate (?P<target>-?[0-9]{1,20})",
        ("values", "target"),
    ),
)


def _span(text: str, field: str, bounds: tuple[int, int]) -> SourceSpan:
    start, stop = bounds
    return SourceSpan(
        field, bounds, (len(text[:start].encode()), len(text[:stop].encode())), text[start:stop]
    )


class HumanInterpreter:
    def __init__(self, ideons: IdeonController) -> None:
        self._ideons = ideons

    @staticmethod
    def templates_snapshot() -> bytes:
        return canonical(
            {"schema": "af.human-templates@1", "templates": [asdict(item) for item in TEMPLATES]}
        )

    def interpret(
        self, observation: ObservedSignal, context: ContextGraph | None = None
    ) -> HumanInterpretation:
        text = observation.data.decode("utf-8")
        ideon = self._ideons.interpret(
            InterpretRequest(
                surface=text,
                source_id=observation.source_id,
                rights=observation.rights,
                context=context or ContextGraph(),
                action_requested=False,
            )
        )
        if observation.source_kind is not SourceKind.USER or text.lstrip().startswith(
            ('"', "'", "`", ">")
        ):
            return HumanInterpretation(
                observation, ideon, "quarantined", None, ("source_is_data_not_request",)
            )
        for template in TEMPLATES:
            match = re.fullmatch(template.expression, text, re.ASCII | re.IGNORECASE)
            if match is None:
                continue
            try:
                arguments = {
                    slot: parse_json(match.group(slot).encode()) for slot in template.slots
                }
                data = canonical(arguments)
                hypothesis = ProblemCatalogue.match(template.problem, data)
                if hypothesis.status != "matched":
                    raise AlgorithmError("template arguments invalid")
                # The human seed intentionally uses a smaller bound than direct algorithm inputs.
                if isinstance(arguments["values"], list) and len(arguments["values"]) > 64:
                    raise AlgorithmError("human template item bound exceeded")
            except AlgorithmError:
                return HumanInterpretation(
                    observation, ideon, "rejected", None, ("invalid_template_arguments",)
                )
            what = WhatContract(
                template.problem,
                data,
                observation.digest,
                template.id,
                template.digest,
                (
                    _span(text, "problem", match.span()),
                    *(_span(text, slot, match.span(slot)) for slot in template.slots),
                ),
            )
            return HumanInterpretation(observation, ideon, "accepted", what, ())
        return HumanInterpretation(
            observation,
            ideon,
            "needs_clarification",
            None,
            ideon.openings or ("supported_problem_and_typed_inputs",),
        )

    @staticmethod
    def _what(interpretation: HumanInterpretation) -> WhatContract:
        what = interpretation.what
        if (
            interpretation.state != "accepted"
            or what is None
            or what.observation_digest != interpretation.observation.digest
        ):
            raise InterpreterError("a validated WHAT contract is required")
        return what

    def assess(
        self,
        interpretation: HumanInterpretation,
        context: ContextGraph,
        should: ShouldPort,
        how: HowPort,
    ) -> MaskAssessment:
        what = self._what(interpretation)
        permission = should.assess(what, context)
        if permission.what_digest != what.digest:
            raise InterpreterError("SHOULD evidence belongs to a different WHAT contract")
        if permission.status != "permit":
            return MaskAssessment(what.digest, permission, None)
        feasibility = how.assess(what, context)
        if feasibility.what_digest != what.digest:
            raise InterpreterError("HOW evidence changed the WHAT contract")
        return MaskAssessment(what.digest, permission, feasibility)

    def render(
        self,
        interpretation: HumanInterpretation,
        value: object,
        audience: str,
        release: ReleasePort,
    ) -> RenderedSignal:
        what = self._what(interpretation)
        try:
            encoded = canonical(value)
            # Bounded JSON also rejects NaN, invalid types and overlarge result envelopes.
            parsed = parse_json(encoded)
            if not ProblemCatalogue.verify(what.problem, what.arguments_json, parsed):
                raise InterpreterError("outbound verification failed")
        except (AlgorithmError, ValueError, TypeError, RecursionError) as exc:
            raise InterpreterError("outbound verification failed") from exc
        claim = OutboundClaim(what.digest, what.problem, encoded)
        permission = release.decide(claim, audience)
        if (
            permission.claim_digest != claim.digest
            or permission.audience != audience
            or permission.permitted is not True
        ):
            raise InterpreterError("outbound release not permitted for this claim and audience")
        return RenderedSignal(
            f"{what.problem}: {encoded.decode()}".encode(),
            audience,
            claim.digest,
            permission.policy_id,
        )
