"""Independent verification for registered reference procedures."""

from __future__ import annotations

from af.framework.contracts import Procedure
from af.framework.types import (
    Classification,
    ProcedureResult,
    VerificationResult,
    WorkRequest,
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
            text = classification.arguments["text"]
            character = classification.arguments["character"].casefold()
            passed = result.output == str(
                sum(1 for candidate in text if candidate.casefold() == character)
            )
            return VerificationResult(
                passed,
                "independent count matched" if passed else "independent count mismatch",
            )
        return VerificationResult(False, "no verifier is registered for this intent")
