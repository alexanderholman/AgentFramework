"""Provider-contributed deterministic procedure helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.contracts import Classifier, Procedure
from af.framework.errors import PlatformError, PlatformReason, ServiceNotBound
from af.framework.types import Classification, ProcedureResult, VerificationResult, WorkRequest

PROCEDURE_CONTRIBUTORS_TAG = "af.procedure.contributors"


class ProcedureResultVerifier(Protocol):
    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult | None: ...


@dataclass(frozen=True, slots=True)
class InstalledProcedureContribution:
    classifier: Classifier
    procedure: Procedure
    verifier: ProcedureResultVerifier


def bind_procedure_contributor(
    registrar: ProviderRegistrar,
    contributor: InstalledProcedureContribution,
) -> None:
    token = object()
    registrar.bind(token, lambda _: contributor, scope=Scope.APPLICATION, owned=False)
    registrar.tag(PROCEDURE_CONTRIBUTORS_TAG, token)


def resolve_procedure_contributions(
    resolver: Resolver,
) -> tuple[InstalledProcedureContribution, ...]:
    try:
        contributors = resolver.resolve_tagged(PROCEDURE_CONTRIBUTORS_TAG)
    except ServiceNotBound:
        return ()
    validated: list[InstalledProcedureContribution] = []
    for contributor in contributors:
        if not isinstance(contributor, InstalledProcedureContribution):
            raise PlatformError(
                PlatformReason.CONFIGURATION_INVALID,
                stage="framework.procedure-contributors",
            )
        validated.append(contributor)
    return tuple(validated)
