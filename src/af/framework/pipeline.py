"""The AF execution kernel."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from af.framework.contracts import (
    Classifier,
    RegressionGate,
    ReuseStore,
    TraceRepository,
    Verifier,
)
from af.framework.errors import UnsupportedWork, VerificationFailed
from af.framework.routing import LadderRouter
from af.framework.types import (
    ExecutionOutcome,
    RunTrace,
    TraceEvent,
    WorkRequest,
)


class PipelineAgent:
    """Coordinate AF capabilities; it contains no domain-specific work logic."""

    def __init__(
        self,
        *,
        classifier: Classifier,
        router: LadderRouter,
        verifier: Verifier,
        traces: TraceRepository,
        regression_gate: RegressionGate,
        reuse_store: ReuseStore,
    ) -> None:
        self._classifier = classifier
        self._router = router
        self._verifier = verifier
        self._traces = traces
        self._regression_gate = regression_gate
        self._reuse_store = reuse_store

    def handle(self, request: WorkRequest) -> ExecutionOutcome:
        trace_id = str(uuid4())
        created_at = datetime.now(UTC)
        events: list[TraceEvent] = []

        classification = self._classifier.classify(request)
        if classification is None:
            events.append(TraceEvent("classify", "unsupported"))
            events.append(TraceEvent("record", "unsupported trace"))
            self._traces.save(
                RunTrace(
                    id=trace_id,
                    prompt=request.prompt,
                    intent=None,
                    arguments={},
                    route=None,
                    tier=None,
                    output=None,
                    verified=False,
                    reused=False,
                    regression_passed=False,
                    promoted=False,
                    status="unsupported",
                    events=tuple(events),
                    created_at=created_at,
                )
            )
            raise UnsupportedWork(request.prompt, trace_id)
        events.append(TraceEvent("classify", classification.intent))

        try:
            procedure = self._router.route(classification, prompt=request.prompt)
        except UnsupportedWork as error:
            events.append(TraceEvent("route", "not found"))
            events.append(TraceEvent("record", "unsupported trace"))
            self._traces.save(
                RunTrace(
                    id=trace_id,
                    prompt=request.prompt,
                    intent=classification.intent,
                    arguments=classification.arguments,
                    route=None,
                    tier=None,
                    output=None,
                    verified=False,
                    reused=False,
                    regression_passed=False,
                    promoted=False,
                    status="unsupported",
                    events=tuple(events),
                    created_at=created_at,
                )
            )
            raise UnsupportedWork(request.prompt, trace_id) from error
        events.append(TraceEvent("route", procedure.id))

        result = self._reuse_store.lookup(classification, procedure)
        reused = result is not None
        if result is None:
            result = procedure.execute(classification)
        events.append(TraceEvent("execute", "reused" if reused else procedure.id))

        verification = self._verifier.verify(request, classification, procedure, result)
        events.append(TraceEvent("verify", verification.reason))
        events.append(TraceEvent("record", "trace captured"))

        regression_passed = verification.passed and (
            reused
            or self._regression_gate.evaluate(
                request,
                classification,
                procedure,
                result,
                self._verifier,
            )
        )
        events.append(TraceEvent("test", "passed" if regression_passed else "not promotable"))

        promoted = verification.passed and regression_passed and not reused
        if promoted:
            self._reuse_store.promote(classification, procedure, result)
        events.append(
            TraceEvent("reuse", "hit" if reused else "promoted" if promoted else "skipped")
        )

        trace = RunTrace(
            id=trace_id,
            prompt=request.prompt,
            intent=classification.intent,
            arguments=classification.arguments,
            route=procedure.id,
            tier=procedure.tier.name.lower(),
            output=result.output,
            verified=verification.passed,
            reused=reused,
            regression_passed=regression_passed,
            promoted=promoted,
            status="completed" if verification.passed else "failed",
            events=tuple(events),
            created_at=created_at,
        )
        self._traces.save(trace)

        if not verification.passed:
            raise VerificationFailed(trace_id, verification.reason)

        return ExecutionOutcome(
            prompt=request.prompt,
            output=result.output,
            intent=classification.intent,
            route=procedure.id,
            tier=procedure.tier.name.lower(),
            status="completed",
            verified=True,
            reused=reused,
            trace_id=trace_id,
        )
