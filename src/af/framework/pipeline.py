"""The AF execution kernel."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from af.framework.contracts import (
    Classifier,
    RegressionGate,
    ReuseStore,
    Selector,
    TraceRepository,
    Verifier,
)
from af.framework.errors import InvalidSelectionProposal, UnsupportedWork, VerificationFailed
from af.framework.routing import LadderRouter, ProcedureRegistry
from af.framework.types import (
    Classification,
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
        registry: ProcedureRegistry,
        router: LadderRouter,
        verifier: Verifier,
        traces: TraceRepository,
        regression_gate: RegressionGate,
        reuse_store: ReuseStore,
        selector: Selector | None = None,
        selector_confidence_threshold: float = 0.8,
    ) -> None:
        self._classifier = classifier
        self._registry = registry
        self._router = router
        self._verifier = verifier
        self._traces = traces
        self._regression_gate = regression_gate
        self._reuse_store = reuse_store
        self._selector = selector
        self._selector_confidence_threshold = selector_confidence_threshold

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
        classification, expected_procedure_id = self._resolve_classification(
            request,
            classification,
            trace_id=trace_id,
            created_at=created_at,
            events=events,
        )

        try:
            procedure = self._router.route(classification, prompt=request.prompt)
        except UnsupportedWork as error:
            events.append(TraceEvent("route", "not found"))
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id) from error
        if expected_procedure_id is not None and procedure.id != expected_procedure_id:
            events.append(
                TraceEvent(
                    "select",
                    f"selector proposal did not match routed procedure: {expected_procedure_id}",
                )
            )
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id)
        try:
            procedure.validate(classification)
        except InvalidSelectionProposal as error:
            events.append(TraceEvent("route", "invalid classification"))
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
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

    def _resolve_classification(
        self,
        request: WorkRequest,
        classification: Classification,
        *,
        trace_id: str,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> tuple[Classification, str | None]:
        if classification.intent != "unknown":
            return classification, None
        if self._selector is None:
            events.append(TraceEvent("select", "human handoff: no selector installed"))
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id)

        decision = self._selector.select(
            request.prompt,
            tuple(procedure.id for procedure in self._registry.all()),
        )
        if decision.proposal is None:
            reason = decision.reason or "selector requested human handoff"
            events.append(
                TraceEvent(
                    "select",
                    f"{self._selector.name} [{self._selector.locality}] handoff: {reason}",
                )
            )
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id)

        proposal = decision.proposal
        if proposal.confidence < self._selector_confidence_threshold:
            events.append(
                TraceEvent(
                    "select",
                    (
                        f"{self._selector.name} [{self._selector.locality}] handoff: "
                        f"confidence {proposal.confidence:.2f} below threshold "
                        f"{self._selector_confidence_threshold:.2f}"
                    ),
                )
            )
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id)

        procedure = self._registry.get(proposal.procedure_id)
        if procedure is None:
            events.append(
                TraceEvent(
                    "select",
                    (
                        f"{self._selector.name} [{self._selector.locality}] handoff: "
                        f"unknown procedure {proposal.procedure_id}"
                    ),
                )
            )
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id)

        selected_classification = Classification(
            intent=proposal.intent,
            arguments=proposal.arguments,
            confidence=proposal.confidence,
            classifier=self._selector.name,
        )
        if not procedure.supports(selected_classification):
            events.append(
                TraceEvent(
                    "select",
                    (
                        f"{self._selector.name} [{self._selector.locality}] handoff: "
                        f"unsupported intent {proposal.intent}"
                    ),
                )
            )
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id)

        try:
            procedure.validate(selected_classification)
        except InvalidSelectionProposal as error:
            events.append(
                TraceEvent(
                    "select",
                    (f"{self._selector.name} [{self._selector.locality}] handoff: {error}"),
                )
            )
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id) from error

        events.append(
            TraceEvent(
                "select",
                (
                    f"{self._selector.name} [{self._selector.locality}] selected "
                    f"{proposal.procedure_id}/{proposal.intent} "
                    f"confidence={proposal.confidence:.2f}"
                ),
            )
        )
        return selected_classification, proposal.procedure_id

    def _record_unsupported_trace(
        self,
        *,
        trace_id: str,
        request: WorkRequest,
        classification: Classification | None,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> None:
        events.append(TraceEvent("record", "unsupported trace"))
        self._traces.save(
            RunTrace(
                id=trace_id,
                prompt=request.prompt,
                intent=None if classification is None else classification.intent,
                arguments={} if classification is None else classification.arguments,
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
