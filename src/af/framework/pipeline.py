"""The AF execution kernel."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Never
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
    ProcedureDescriptor,
    RunTrace,
    SelectionDecision,
    SelectionProposal,
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
            self._fail_closed_selection(
                "human handoff: no selector installed",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        selector_label = self._selector_label()
        threshold = self._validated_probability(
            self._selector_confidence_threshold,
            error_detail=f"{selector_label} handoff: invalid selector threshold",
            trace_id=trace_id,
            request=request,
            classification=classification,
            created_at=created_at,
            events=events,
        )
        try:
            decision = self._selector.select(request.prompt, self._registry.catalogue())
        except Exception:
            self._fail_closed_selection(
                f"{selector_label} handoff: selector failure",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        proposal, handoff_reason = self._normalize_selection_decision(
            decision,
            selector_label=selector_label,
            trace_id=trace_id,
            request=request,
            classification=classification,
            created_at=created_at,
            events=events,
        )
        if proposal is None:
            self._fail_closed_selection(
                f"{selector_label} handoff: {handoff_reason}",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        validated_confidence = self._validated_probability(
            proposal.confidence,
            error_detail=f"{selector_label} handoff: invalid confidence",
            trace_id=trace_id,
            request=request,
            classification=classification,
            created_at=created_at,
            events=events,
        )
        if validated_confidence < threshold:
            self._fail_closed_selection(
                (
                    f"{selector_label} handoff: confidence {validated_confidence:.2f} "
                    f"below threshold {threshold:.2f}"
                ),
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        procedure = self._registry.get(proposal.procedure_id)
        if procedure is None:
            self._fail_closed_selection(
                f"{selector_label} handoff: unknown procedure {proposal.procedure_id}",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        if proposal.intent != procedure.selection_descriptor.intent:
            self._fail_closed_selection(
                f"{selector_label} handoff: unsupported intent {proposal.intent}",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        selected_classification = Classification(
            intent=proposal.intent,
            arguments=self._validate_selection_arguments(
                procedure.selection_descriptor,
                proposal.arguments,
                selector_label=selector_label,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            ),
            confidence=validated_confidence,
            classifier=self._selector.name,
        )
        if not procedure.supports(selected_classification):
            self._fail_closed_selection(
                f"{selector_label} handoff: unsupported intent {proposal.intent}",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        try:
            procedure.validate(selected_classification)
        except InvalidSelectionProposal as error:
            self._fail_closed_selection(
                f"{selector_label} handoff: {error}",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        events.append(
            TraceEvent(
                "select",
                (
                    f"{selector_label} selected "
                    f"{proposal.procedure_id}/{proposal.intent} "
                    f"confidence={validated_confidence:.2f}"
                ),
            )
        )
        return selected_classification, proposal.procedure_id

    def _normalize_selection_decision(
        self,
        decision: SelectionDecision,
        *,
        selector_label: str,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> tuple[SelectionProposal | None, str | None]:
        if not isinstance(decision, SelectionDecision):
            self._fail_closed_selection(
                f"{selector_label} handoff: invalid selector decision",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        proposal = decision.proposal
        reason = decision.reason
        if proposal is not None and not isinstance(proposal, SelectionProposal):
            self._fail_closed_selection(
                f"{selector_label} handoff: invalid selector decision",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        if reason is not None and not isinstance(reason, str):
            self._fail_closed_selection(
                f"{selector_label} handoff: invalid selector decision",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        if proposal is not None and (
            not isinstance(proposal.procedure_id, str) or not isinstance(proposal.intent, str)
        ):
            self._fail_closed_selection(
                f"{selector_label} handoff: invalid selector decision",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        normalized_reason = None if reason is None else reason.strip()
        has_proposal = proposal is not None
        has_reason = normalized_reason is not None and normalized_reason != ""
        if has_proposal == has_reason:
            self._fail_closed_selection(
                f"{selector_label} handoff: invalid selector decision",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        return proposal, normalized_reason

    def _validate_selection_arguments(
        self,
        descriptor: ProcedureDescriptor,
        arguments: dict[str, str],
        *,
        selector_label: str,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> dict[str, str]:
        if not isinstance(arguments, dict):
            self._fail_closed_selection(
                f"{selector_label} handoff: invalid arguments for {descriptor.procedure_id}",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        expected_arguments = {argument.name: argument for argument in descriptor.arguments}
        if set(arguments) != set(expected_arguments):
            self._fail_closed_selection(
                f"{selector_label} handoff: invalid arguments for {descriptor.procedure_id}",
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        validated: dict[str, str] = {}
        for name, schema in expected_arguments.items():
            value = arguments[name]
            if not isinstance(value, schema.value_type):
                self._fail_closed_selection(
                    f"{selector_label} handoff: invalid arguments for {descriptor.procedure_id}",
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )
            if not schema.allow_blank and not value.strip():
                self._fail_closed_selection(
                    f"{selector_label} handoff: invalid arguments for {descriptor.procedure_id}",
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )
            if len(value) < schema.min_length or (
                schema.max_length is not None and len(value) > schema.max_length
            ):
                self._fail_closed_selection(
                    f"{selector_label} handoff: invalid arguments for {descriptor.procedure_id}",
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )
            validated[name] = value
        return validated

    def _validated_probability(
        self,
        value: float,
        *,
        error_detail: str,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            self._fail_closed_selection(
                error_detail,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        normalized = float(value)
        if normalized < 0.0 or normalized > 1.0:
            self._fail_closed_selection(
                error_detail,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        return normalized

    def _selector_label(self) -> str:
        if self._selector is None:
            return "selector"
        return f"{self._selector.name} [{self._selector.locality}]"

    def _fail_closed_selection(
        self,
        detail: str,
        *,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> Never:
        events.append(TraceEvent("select", detail))
        self._record_unsupported_trace(
            trace_id=trace_id,
            request=request,
            classification=classification,
            created_at=created_at,
            events=events,
        )
        raise UnsupportedWork(request.prompt, trace_id)

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
