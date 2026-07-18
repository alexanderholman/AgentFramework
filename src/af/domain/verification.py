"""Independent verification for registered reference procedures."""

from __future__ import annotations

from af.framework.contracts import Procedure, Verifier
from af.framework.types import (
    Classification,
    ProcedureResult,
    VerificationResult,
    WorkRequest,
    WorkTier,
)


class DeterministicVerifier:
    """Use an implementation independent of the procedure's explicit loop."""

    def __init__(self) -> None:
        self.force_failure = False

    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult:
        del request, procedure
        if self.force_failure:
            return VerificationResult(False, "forced test failure")
        if classification.intent == "system.hello":
            passed = result.output == "Hello, world!"
            return VerificationResult(
                passed, "static greeting matched" if passed else "greeting mismatch"
            )
        if classification.intent == "text.character_count":
            text = classification.arguments["text"].casefold()
            character = classification.arguments["character"].casefold()
            passed = result.output == str(text.count(character))
            return VerificationResult(
                passed,
                "independent count matched" if passed else "independent count mismatch",
            )
        return VerificationResult(False, "no verifier is registered for this intent")


class VerifierRouter:
    """Dispatch generative work only to an explicitly installed verifier."""

    def __init__(
        self,
        deterministic: Verifier,
        generative: Verifier | None = None,
    ) -> None:
        self._deterministic = deterministic
        self._generative = generative

    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult:
        if procedure.tier is WorkTier.GENERATIVE and self._generative is not None:
            return self._generative.verify(request, classification, procedure, result)
        return self._deterministic.verify(request, classification, procedure, result)
