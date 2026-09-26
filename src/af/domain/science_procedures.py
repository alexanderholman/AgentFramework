"""Portable AF classifier/procedure/verifier contribution for the science catalogue."""

from __future__ import annotations

import json
import re
from fractions import Fraction

from af.domain.procedures import BaseProcedure, PureProcedureMixin
from af.domain.science import CalculationResult, calculate, parse_request, verify_result
from af.domain.science.catalogue import CATALOGUE_VERSION, MAX_PROMPT
from af.domain.science.quantities import ScienceInputError, ScienceReason
from af.framework.container import ProviderRegistrar, ServiceProvider
from af.framework.contracts import Procedure
from af.framework.procedure_contributors import (
    InstalledProcedureContribution,
    bind_procedure_contributor,
)
from af.framework.types import (
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


class ScienceClassifier:
    def classify(self, request: WorkRequest) -> Classification | None:
        if not re.match(r"^(math|physics|calculate)\s", request.prompt.strip(), re.ASCII):
            return None
        return Classification(
            intent="science.calculate",
            arguments={"expression": request.prompt.strip()},
            confidence=1.0,
            classifier=type(self).__name__,
        )


class ScienceProcedure(PureProcedureMixin, BaseProcedure):
    id = "science-calculate"
    version = CATALOGUE_VERSION
    tier = WorkTier.PROCEDURAL
    metrics = RouteMetrics(cost=1, latency=1, intelligence=0)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=version,
        intent="science.calculate",
        arguments=(
            ProcedureArgumentDescriptor(
                name="expression",
                value_type=ArgumentValueType.TEXT,
                min_length=1,
                max_length=MAX_PROMPT,
                allow_blank=False,
            ),
        ),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
        requires_confirmation=False,
    )

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "science.calculate"

    def validate(self, classification: Classification) -> None:
        if not self.supports(classification) or set(classification.arguments) != {"expression"}:
            raise ScienceInputError(ScienceReason.INVALID_ARGUMENTS)
        parse_request(classification.arguments["expression"])

    def execute(self, classification: Classification) -> ProcedureResult:
        self.validate(classification)
        request = parse_request(classification.arguments["expression"])
        result = calculate(request)
        payload = result.to_dict()
        return ProcedureResult(
            output=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            evidence=payload,
        )


class ScienceVerifier:
    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult | None:
        del request
        if classification.intent != "science.calculate":
            return None
        passed = False
        try:
            if (
                procedure.id != ScienceProcedure.id
                or procedure.version != CATALOGUE_VERSION
                or len(result.output) > 32768
                or set(classification.arguments) != {"expression"}
            ):
                return VerificationResult(False, "science_verification_failed")
            parsed = parse_request(classification.arguments["expression"])
            payload = json.loads(result.output)
            exact = payload["quantity"]["rational"]
            if (
                not isinstance(exact, str)
                or re.fullmatch(r"-?[0-9]{1,2500}(?:/[0-9]{1,2500})?", exact) is None
            ):
                return VerificationResult(False, "science_verification_failed")
            candidate = CalculationResult(
                formula_id=parsed.formula_id,
                formula_digest=parsed.formula.digest,
                exact_value=Fraction(exact),
                unit=parsed.output_unit,
                substitutions=parsed.arguments,
            )
            passed = (
                verify_result(parsed, candidate)
                and result.output
                == json.dumps(candidate.to_dict(), sort_keys=True, separators=(",", ":"))
                and result.evidence == candidate.to_dict()
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            ZeroDivisionError,
            RecursionError,
            ScienceInputError,
        ):
            passed = False
        return VerificationResult(
            passed, "science_verified" if passed else "science_verification_failed"
        )


class ScienceProvider(ServiceProvider):
    provider_id = "af.science"

    def register(self, registrar: ProviderRegistrar) -> None:
        bind_procedure_contributor(
            registrar,
            InstalledProcedureContribution(
                classifier=ScienceClassifier(),
                procedure=ScienceProcedure(),
                verifier=ScienceVerifier(),
            ),
        )
