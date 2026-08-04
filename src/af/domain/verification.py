"""Independent verification for registered reference procedures."""

from __future__ import annotations

from collections.abc import Iterable

from af.framework.contracts import Procedure
from af.framework.procedure_contributors import ProcedureResultVerifier
from af.framework.types import (
    Classification,
    ProcedureResult,
    VerificationResult,
    WorkRequest,
)


class DeterministicVerifier:
    """Use an implementation independent of the procedure's explicit loop."""

    def __init__(self, verifiers: Iterable[ProcedureResultVerifier] = ()) -> None:
        self.force_failure = False
        self._verifiers = tuple(verifiers)

    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult:
        if self.force_failure:
            return VerificationResult(False, "forced test failure")
        if classification.intent == "system.hello":
            del request, procedure
            passed = result.output == "Hello, world!"
            return VerificationResult(
                passed, "static greeting matched" if passed else "greeting mismatch"
            )
        if classification.intent == "text.character_count":
            del request, procedure
            text = classification.arguments["text"]
            character = classification.arguments["character"].casefold()
            passed = result.output == str(
                sum(1 for candidate in text if candidate.casefold() == character)
            )
            return VerificationResult(
                passed,
                "independent count matched" if passed else "independent count mismatch",
            )
        if classification.intent == "system.current_time":
            del request, procedure
            try:
                from datetime import datetime

                iso = result.output
                datetime.fromisoformat(iso)
                passed = True
            except (ValueError, TypeError):
                passed = False
            return VerificationResult(
                passed,
                "valid ISO 8601 timestamp" if passed else "invalid ISO 8601 timestamp",
            )
        for verifier in self._verifiers:
            verdict = verifier.verify(request, classification, procedure, result)
            if verdict is not None:
                return verdict
        return VerificationResult(False, "no verifier is registered for this intent")
