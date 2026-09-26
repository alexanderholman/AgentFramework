"""The admitted catalogue becomes a digest-pinned deterministic direct route."""

from __future__ import annotations

from af.application.engineering import LocalEngineeringService
from af.domain.procedures import BaseProcedure
from af.framework.context import current_context
from af.framework.contracts import Procedure
from af.framework.engineering import FAMILY
from af.framework.errors import InvalidSelectionProposal
from af.framework.procedure_contributors import InstalledProcedureContribution
from af.framework.types import (
    PROMPT_MAX_LENGTH,
    ArgumentValueType,
    Classification,
    ProcedureArgumentDescriptor,
    ProcedureDescriptor,
    ProcedureEffect,
    ProcedureResult,
    ProcedureRisk,
    RouteMetrics,
    VerificationResult,
    WorkRequest,
    WorkTier,
)

_PREFIX = "count nonblank lines:"
_INTENT = "text.admitted_nonblank_line_total"


class AdmittedLineClassifier:
    def __init__(self, service: LocalEngineeringService) -> None:
        self._service = service

    def classify(self, request: WorkRequest) -> Classification | None:
        if not request.prompt.startswith(_PREFIX):
            return None
        text = request.prompt[len(_PREFIX) :]
        if len(text.encode()) > 60_000:
            return None
        admission = self._service.admitted(FAMILY)
        if admission is None:
            return None
        return Classification(
            _INTENT,
            {"text": text, "admission": admission.digest},
            1.0,
            "admitted-line-classifier",
        )


class AdmittedLineProcedure(BaseProcedure):
    id = "admitted-nonblank-line-total"
    tier = WorkTier.PROCEDURAL
    replay_safe = True
    # Admission can be invalidated. Shared result caching must not bypass catalogue checks.
    pure = False
    has_selection_grounding = True
    metrics = RouteMetrics(cost=1, latency=1, intelligence=0)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=BaseProcedure.version,
        intent=_INTENT,
        arguments=(
            ProcedureArgumentDescriptor(
                "text", ArgumentValueType.TEXT, 0, PROMPT_MAX_LENGTH, allow_blank=True
            ),
            ProcedureArgumentDescriptor("admission", ArgumentValueType.TEXT, 64, 64),
        ),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
    )

    def __init__(self, service: LocalEngineeringService) -> None:
        self._service = service

    def supports(self, classification: Classification) -> bool:
        return classification.intent == _INTENT

    def validate(self, classification: Classification) -> None:
        if not self.supports(classification) or set(classification.arguments) != {
            "text",
            "admission",
        }:
            raise InvalidSelectionProposal("invalid admitted line-count arguments")
        if len(classification.arguments["text"].encode()) > 60_000:
            raise InvalidSelectionProposal("line-count input too large")
        admission = self._service.admitted(FAMILY)
        if admission is None or admission.digest != classification.arguments["admission"]:
            raise InvalidSelectionProposal("admitted line-count route has changed")

    def selection_is_grounded(self, request: WorkRequest, classification: Classification) -> bool:
        return request.prompt == _PREFIX + classification.arguments.get("text", "")

    def execute(self, classification: Classification) -> ProcedureResult:
        self.validate(classification)
        result = self._service.execute_admitted(
            (classification.arguments["text"],),
            classification.arguments["admission"],
            current_context(),
        )
        return ProcedureResult(
            str(result.output),
            {
                "admission": result.admission.digest,
                "component": result.admission.component_digest,
                "workflow": result.admission.definition.digest,
                "run_id": result.run_id,
                "engineered": False,
                "engineering_seconds": 0.0,
                "execution_seconds": result.execution_seconds,
                "model_calls": 0,
            },
        )


class AdmittedLineVerifier:
    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult | None:
        if classification.intent != _INTENT:
            return None
        text = classification.arguments.get("text", "")
        expected = sum(bool(line.strip()) for line in text.split("\n"))
        passed = (
            procedure.id == AdmittedLineProcedure.id
            and request.prompt == _PREFIX + text
            and result.output == str(expected)
            and result.evidence.get("admission") == classification.arguments.get("admission")
            and result.evidence.get("engineered") is False
        )
        return VerificationResult(
            passed, "admitted-line-verified" if passed else "line-verification-failed"
        )


def engineering_contribution(service: LocalEngineeringService) -> InstalledProcedureContribution:
    return InstalledProcedureContribution(
        AdmittedLineClassifier(service), AdmittedLineProcedure(service), AdmittedLineVerifier()
    )
