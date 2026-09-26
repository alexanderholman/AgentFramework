"""Content minimization for observational traces, independent of Task storage."""

from __future__ import annotations

from dataclasses import replace

from af.framework.contracts import TraceRepository
from af.framework.types import RunTrace, TraceEvent

REDACTED = "[redacted]"


def redact_trace(trace: RunTrace) -> RunTrace:
    """Retain execution metadata without duplicating prompt or result content.

    Route/provider identifiers and finite selection reason codes remain observable.
    Free-form verifier explanations are replaced by the verified outcome. No prompt
    hash is retained: hashes of short/private requests can be dictionary attacked.
    """
    return replace(
        trace,
        prompt=REDACTED,
        arguments={},
        output=None if trace.output is None else REDACTED,
        events=tuple(
            TraceEvent(event.stage, "passed" if trace.verified else "failed")
            if event.stage == "verify"
            else event
            for event in trace.events
        ),
    )


class RedactingTraceRepository:
    """Apply minimization even when a host supplies its own trace storage port."""

    def __init__(self, repository: TraceRepository) -> None:
        self._repository = repository

    def save(self, trace: RunTrace) -> None:
        self._repository.save(redact_trace(trace))

    def get(self, trace_id: str) -> RunTrace:
        return redact_trace(self._repository.get(trace_id))

    def list(self) -> list[RunTrace]:
        return [redact_trace(trace) for trace in self._repository.list()]
