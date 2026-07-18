"""Regression-gated reuse of pure procedure outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass

from af.framework.contracts import Procedure, Verifier
from af.framework.types import Classification, ProcedureResult, WorkRequest


@dataclass(frozen=True, slots=True)
class ReuseKey:
    intent: str
    arguments: str
    procedure_id: str
    procedure_version: str


def reuse_key(classification: Classification, procedure: Procedure) -> ReuseKey:
    return ReuseKey(
        intent=classification.intent,
        arguments=json.dumps(classification.arguments, sort_keys=True, separators=(",", ":")),
        procedure_id=procedure.id,
        procedure_version=procedure.version,
    )


class InMemoryReuseStore:
    def __init__(self) -> None:
        self._results: dict[ReuseKey, ProcedureResult] = {}

    def lookup(
        self, classification: Classification, procedure: Procedure
    ) -> ProcedureResult | None:
        return self._results.get(reuse_key(classification, procedure))

    def promote(
        self,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> None:
        self._results[reuse_key(classification, procedure)] = result


class ReplayRegressionGate:
    """Replay cheap pure procedures before allowing their output into reuse storage."""

    def evaluate(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
        verifier: Verifier,
    ) -> bool:
        if not procedure.pure or not procedure.replay_safe:
            return False
        replay = procedure.execute(classification)
        if replay.output != result.output:
            return False
        return verifier.verify(request, classification, procedure, replay).passed
