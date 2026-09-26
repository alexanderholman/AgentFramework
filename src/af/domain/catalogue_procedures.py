"""One provider contribution for bounded catalogue requests on every AF transport."""

from __future__ import annotations

import re

from af.domain.algorithms import AlgorithmError, ReferenceAlgorithms
from af.domain.algorithms.contracts import VERSION, canonical, parse_input
from af.domain.problems import ProblemCatalogue
from af.domain.procedures import BaseProcedure, PureProcedureMixin
from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.framework.contracts import Procedure
from af.framework.procedure_contributors import (
    InstalledProcedureContribution,
    bind_procedure_contributor,
)
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

_PATTERN = re.compile(r"^(algorithm|problem) ([a-z][a-z-]{0,63}) (\{.*\})$", re.DOTALL | re.ASCII)


def parse_command(text: str) -> tuple[str, str, bytes]:
    if not isinstance(text, str) or len(text) > PROMPT_MAX_LENGTH:
        raise AlgorithmError("invalid catalogue request bound")
    match = _PATTERN.fullmatch(text)
    if match is None:
        raise AlgorithmError("use algorithm or problem, installed id, and a JSON object")
    mode, name, data = match.groups()
    if mode == "algorithm":
        parse_input(name, data.encode())
    elif ProblemCatalogue.match(name, data.encode()).status != "matched":
        raise AlgorithmError("problem request does not satisfy its input contract")
    else:
        # Prompt grammar uses canonical IDs. Aliases are explicit recognition-only proposals.
        ProblemCatalogue.descriptor(name)
    return mode, name, data.encode()


class CatalogueClassifier:
    def classify(self, request: WorkRequest) -> Classification | None:
        if not request.prompt.startswith(("algorithm ", "problem ")):
            return None
        return Classification(
            "catalogue.execute", {"request": request.prompt}, 1.0, type(self).__name__
        )


class CatalogueProcedure(PureProcedureMixin, BaseProcedure):
    id = "catalogue-execute"
    version = VERSION
    tier = WorkTier.PROCEDURAL
    metrics = RouteMetrics(cost=1, latency=1, intelligence=0)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=VERSION,
        intent="catalogue.execute",
        arguments=(
            ProcedureArgumentDescriptor("request", ArgumentValueType.TEXT, 1, PROMPT_MAX_LENGTH),
        ),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
    )

    def __init__(self, catalogue: ReferenceAlgorithms | None = None) -> None:
        self._catalogue = catalogue or ReferenceAlgorithms()

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "catalogue.execute"

    def validate(self, classification: Classification) -> None:
        if not self.supports(classification) or set(classification.arguments) != {"request"}:
            raise AlgorithmError("unsupported catalogue intent or arguments")
        parse_command(classification.arguments["request"])

    def execute(self, classification: Classification) -> ProcedureResult:
        self.validate(classification)
        mode, name, data = parse_command(classification.arguments["request"])
        if mode == "algorithm":
            payload = self._catalogue.execute(name, data).to_dict()
        else:
            payload = ProblemCatalogue(self._catalogue).resolve(name, data)
        return ProcedureResult(canonical(payload).decode(), payload)


class CatalogueVerifier:
    def __init__(self, catalogue: ReferenceAlgorithms | None = None) -> None:
        self._catalogue = catalogue or ReferenceAlgorithms()

    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult | None:
        if classification.intent != "catalogue.execute":
            return None
        passed = False
        try:
            if (
                procedure.id != CatalogueProcedure.id
                or procedure.version != VERSION
                or classification.arguments != {"request": request.prompt}
            ):
                return VerificationResult(False, "catalogue_verification_failed")
            # Rebuild the complete deterministic evidence envelope. Every result is checked
            # by independent semantic oracles, including whole-problem acceptance.
            expected = CatalogueProcedure(self._catalogue).execute(classification)
            passed = result.output == expected.output and result.evidence == expected.evidence
        except (AlgorithmError, ValueError, TypeError, KeyError):
            passed = False
        return VerificationResult(
            passed, "catalogue_verified" if passed else "catalogue_verification_failed"
        )


class CatalogueProvider(ServiceProvider):
    provider_id = "af.catalogues"

    def register(self, registrar: ProviderRegistrar) -> None:
        catalogue = ReferenceAlgorithms()
        registrar.bind(
            ReferenceAlgorithms, lambda _: catalogue, scope=Scope.APPLICATION, owned=False
        )
        registrar.bind(
            ProblemCatalogue, lambda _: ProblemCatalogue(catalogue), scope=Scope.APPLICATION
        )
        bind_procedure_contributor(
            registrar,
            InstalledProcedureContribution(
                CatalogueClassifier(), CatalogueProcedure(catalogue), CatalogueVerifier(catalogue)
            ),
        )
