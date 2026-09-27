"""The AF execution kernel."""

from __future__ import annotations

from datetime import UTC, datetime
from math import isfinite
from typing import Never
from uuid import uuid4

from af.framework.classification_hierarchy import FunctionRoute, NoRoute, TracedClassifier
from af.framework.constitution import classification_input, decision_scope, procedure_decision
from af.framework.context import maybe_current_context
from af.framework.contracts import (
    Classifier,
    Procedure,
    RegressionGate,
    ReuseStore,
    Selector,
    TraceRepository,
    Verifier,
)
from af.framework.errors import (
    InvalidPrompt,
    InvalidSelectionProposal,
    InvalidWorkRequest,
    UnsupportedWork,
    VerificationFailed,
)
from af.framework.routing import LadderRouter, ProcedureRegistry
from af.framework.trace_privacy import RedactingTraceRepository
from af.framework.types import (
    PROCEDURE_ARGUMENT_COUNT_MAX,
    PROCEDURE_ID_MAX_LENGTH,
    PROCEDURE_INTENT_MAX_LENGTH,
    PROMPT_MAX_LENGTH,
    WORK_REQUEST_ATTRIBUTE_COUNT_MAX,
    WORK_REQUEST_ATTRIBUTE_KEY_MAX_LENGTH,
    WORK_REQUEST_ATTRIBUTE_NUMBER_MAX_LENGTH,
    ArgumentValueType,
    Classification,
    ExecutionOutcome,
    ProcedureDescriptor,
    ProcedureEffect,
    ProcedureRisk,
    RunTrace,
    SelectionDecision,
    SelectionHandoffReason,
    SelectionProposal,
    SelectionReasonCode,
    SelectorLocality,
    SelectorMetadata,
    TraceEvent,
    WorkRequest,
    is_valid_argument_name,
    is_valid_classifier_id,
    is_valid_procedure_id,
    is_valid_procedure_intent,
    is_valid_procedure_version,
    normalize_prompt,
    parse_probability,
)

_HANDOFF_REASON_CODES = {
    SelectionHandoffReason.CANCELLED: SelectionReasonCode.CANCELLED,
    SelectionHandoffReason.TIMEOUT: SelectionReasonCode.TIMEOUT,
    SelectionHandoffReason.MALFORMED: SelectionReasonCode.MALFORMED,
    SelectionHandoffReason.AMBIGUOUS: SelectionReasonCode.AMBIGUOUS,
    SelectionHandoffReason.UNAVAILABLE: SelectionReasonCode.UNAVAILABLE,
    SelectionHandoffReason.UNSUPPORTED: SelectionReasonCode.UNSUPPORTED,
}


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
        selector_metadata: SelectorMetadata | None = None,
        selector_confidence_threshold: float = 0.8,
    ) -> None:
        self._classifier = classifier
        self._registry = registry
        self._router = router
        self._verifier = verifier
        self._traces = RedactingTraceRepository(traces)
        self._regression_gate = regression_gate
        self._reuse_store = reuse_store
        self._selector = selector
        self._selector_metadata = selector_metadata
        self._selector_confidence_threshold = selector_confidence_threshold

    def handle(self, request: WorkRequest) -> ExecutionOutcome:
        normalized_request = self._normalize_work_request(request, typed_prompt=False)
        return self._handle_request(normalized_request)

    def handle_classification(
        self,
        request: WorkRequest,
        classification: Classification,
        *,
        expected_procedure_id: str | None = None,
    ) -> ExecutionOutcome:
        request, classification, expected_procedure_id = self._normalize_typed_envelope(
            request,
            classification,
            expected_procedure_id=expected_procedure_id,
        )
        return self._handle_request(
            request,
            classification,
            expected_procedure_id=expected_procedure_id,
        )

    def _handle_request(
        self,
        request: WorkRequest,
        classification: Classification | None = None,
        *,
        expected_procedure_id: str | None = None,
    ) -> ExecutionOutcome:
        trace_id = str(uuid4())
        created_at = datetime.now(UTC)
        events: list[TraceEvent] = []
        hierarchy_selector_metadata: SelectorMetadata | None = None

        if classification is None:
            with decision_scope():
                hierarchy = (
                    self._classifier.classify_with_trace(classification_input(request))
                    if isinstance(self._classifier, TracedClassifier)
                    else None
                )
                if hierarchy is None:
                    classification = self._classifier.classify(classification_input(request))
                else:
                    events.extend(hierarchy.events)
                    hierarchy_selector_metadata = hierarchy.selector_metadata
                    if isinstance(hierarchy.outcome, FunctionRoute):
                        proposal = hierarchy.outcome.proposal
                        descriptor = self._registry.descriptor(proposal.procedure_id)
                        if (
                            descriptor is not None
                            and descriptor.version == proposal.version
                            and descriptor.digest == proposal.digest
                        ):
                            classification = Classification(
                                proposal.intent,
                                dict(proposal.arguments),
                                proposal.confidence,
                                hierarchy.visits[-1].node_id,
                            )
                            expected_procedure_id = proposal.procedure_id
                        else:
                            events.append(TraceEvent("classify-stop", "stale_catalogue"))
                    elif isinstance(hierarchy.outcome, NoRoute):
                        classification = Classification("unknown", {}, 0.0, "hierarchy")
        else:
            events.append(TraceEvent("classify", classification.intent))
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
        if not any(event.stage == "classify" for event in events):
            events.append(TraceEvent("classify", classification.intent))

        with decision_scope():
            classification, selected_procedure_id = self._resolve_classification(
                request,
                classification,
                trace_id=trace_id,
                created_at=created_at,
                events=events,
            )
        expected_procedure_id = selected_procedure_id or expected_procedure_id

        return self._execute_classification(
            request,
            classification,
            trace_id=trace_id,
            created_at=created_at,
            events=events,
            expected_procedure_id=expected_procedure_id,
            selector_metadata=hierarchy_selector_metadata,
        )

    def _execute_classification(
        self,
        request: WorkRequest,
        classification: Classification,
        *,
        trace_id: str,
        created_at: datetime,
        events: list[TraceEvent],
        expected_procedure_id: str | None,
        selector_metadata: SelectorMetadata | None = None,
    ) -> ExecutionOutcome:
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
        descriptor = self._registry.descriptor(procedure.id)
        if descriptor is None:
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id)
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
        constitutional = procedure_decision(descriptor)
        if not constitutional.allowed:
            events.append(
                TraceEvent(
                    "constitution-policy",
                    f"{constitutional.reason} constitution={constitutional.constitution_digest}",
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
        classification = Classification(
            intent=classification.intent,
            arguments=self._validate_selection_arguments(
                descriptor,
                classification.arguments,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            ),
            confidence=classification.confidence,
            classifier=classification.classifier,
        )
        invalid_classification = False
        try:
            procedure.validate(classification)
        except InvalidSelectionProposal:
            invalid_classification = True
        if invalid_classification:
            events.append(TraceEvent("route", "invalid classification"))
            self._record_unsupported_trace(
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
            raise UnsupportedWork(request.prompt, trace_id)
        if selector_metadata is not None:
            if classification.confidence < self._selector_confidence_threshold:
                self._fail_closed_selection(
                    SelectionReasonCode.BELOW_THRESHOLD,
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                    selector_metadata=selector_metadata,
                )
            with decision_scope():
                self._enforce_selection_policy(
                    request=request,
                    classification=classification,
                    procedure=procedure,
                    descriptor=descriptor,
                    trace_id=trace_id,
                    created_at=created_at,
                    events=events,
                    selector_metadata=selector_metadata,
                )
        events.append(TraceEvent("route", procedure.id))

        result = self._reuse_store.lookup(classification, procedure)
        reused = result is not None
        context = maybe_current_context()
        stop_reason = (
            SelectionReasonCode.CANCELLED
            if context is not None and context.cancellation.cancelled
            else SelectionReasonCode.TIMEOUT
            if context is not None
            and context.deadline is not None
            and datetime.now(UTC) >= context.deadline
            else None
        )
        if stop_reason is not None:
            self._fail_closed_selection(
                stop_reason,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
                stage="route-policy",
                selector_metadata=selector_metadata,
            )
        if result is None:
            result = procedure.execute(classification)
        events.append(TraceEvent("execute", "reused" if reused else procedure.id))

        verification = self._verifier.verify(request, classification, procedure, result)
        events.append(TraceEvent("verify", verification.reason))
        events.append(TraceEvent("record", "trace captured"))

        regression_passed = verification.passed and (
            reused
            or (
                descriptor.pure
                and descriptor.replay_safe
                and self._regression_gate.evaluate(
                    request,
                    classification,
                    procedure,
                    result,
                    self._verifier,
                )
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
            tier=descriptor.tier.name.lower(),
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
            tier=descriptor.tier.name.lower(),
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
        if self._selector is None or self._selector_metadata is None:
            self._fail_closed_selection(
                SelectionReasonCode.NO_SELECTOR,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        threshold = self._selector_confidence_threshold

        # Raise after the except block so the sanitized UnsupportedWork carries no
        # selector-provider exception context or cause.
        selector_failed = False
        try:
            decision = self._selector.select(request.prompt, self._registry.catalogue())
        except Exception:
            selector_failed = True
            decision = None
        if selector_failed:
            self._fail_closed_selection(
                SelectionReasonCode.SELECTOR_FAILURE,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        proposal, handoff_reason = self._normalize_selection_decision(
            decision,
            trace_id=trace_id,
            request=request,
            classification=classification,
            created_at=created_at,
            events=events,
        )
        if proposal is None:
            if handoff_reason is None:
                self._fail_closed_selection(
                    SelectionReasonCode.INVALID_DECISION,
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )
            handoff_reason_code = _HANDOFF_REASON_CODES.get(handoff_reason)
            if handoff_reason_code is None:
                self._fail_closed_selection(
                    SelectionReasonCode.INVALID_DECISION,
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )
            else:
                self._fail_closed_selection(
                    handoff_reason_code,
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )

        validated_confidence = self._validated_probability(
            proposal.confidence,
            trace_id=trace_id,
            request=request,
            classification=classification,
            created_at=created_at,
            events=events,
        )
        if validated_confidence < threshold:
            self._fail_closed_selection(
                SelectionReasonCode.BELOW_THRESHOLD,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        if not is_valid_procedure_id(proposal.procedure_id):
            self._fail_closed_selection(
                SelectionReasonCode.UNKNOWN_PROCEDURE,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        if not is_valid_procedure_intent(proposal.intent):
            self._fail_closed_selection(
                SelectionReasonCode.UNSUPPORTED_INTENT,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        procedure = self._registry.get(proposal.procedure_id)
        if procedure is None:
            self._fail_closed_selection(
                SelectionReasonCode.UNKNOWN_PROCEDURE,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        descriptor = self._registry.descriptor(procedure.id)
        if descriptor is None:
            self._fail_closed_selection(
                SelectionReasonCode.UNKNOWN_PROCEDURE,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        if (proposal.version is not None and proposal.version != descriptor.version) or (
            proposal.digest is not None and proposal.digest != descriptor.digest
        ):
            self._fail_closed_selection(
                SelectionReasonCode.STALE_PROCEDURE,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        if proposal.intent != descriptor.intent:
            self._fail_closed_selection(
                SelectionReasonCode.UNSUPPORTED_INTENT,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        selected_classification = Classification(
            intent=proposal.intent,
            arguments=self._validate_selection_arguments(
                descriptor,
                proposal.arguments,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            ),
            confidence=validated_confidence,
            classifier=self._selector_metadata.provider_id,
        )
        if not procedure.supports(selected_classification):
            self._fail_closed_selection(
                SelectionReasonCode.UNSUPPORTED_INTENT,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )

        # Raise after the except block so invalid proposal details do not remain in
        # the exception chain of the sanitized handoff.
        invalid_selected_classification = False
        try:
            procedure.validate(selected_classification)
        except InvalidSelectionProposal:
            invalid_selected_classification = True
        if invalid_selected_classification:
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_ARGUMENTS,
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
                    f"provider={self._selector_metadata.provider_id} "
                    f"locality={self._selector_metadata.locality.value} "
                    f"procedure={descriptor.procedure_id} "
                    f"confidence={validated_confidence:.2f}"
                ),
            )
        )
        self._enforce_selection_policy(
            request=request,
            classification=selected_classification,
            procedure=procedure,
            descriptor=descriptor,
            trace_id=trace_id,
            created_at=created_at,
            events=events,
        )
        return selected_classification, proposal.procedure_id

    def _normalize_selection_decision(
        self,
        decision: object,
        *,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> tuple[SelectionProposal | None, SelectionHandoffReason | None]:
        if type(decision) is not SelectionDecision:
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_DECISION,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        proposal = decision.proposal
        reason = decision.reason
        if proposal is not None and type(proposal) is not SelectionProposal:
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_DECISION,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        if reason is not None and type(reason) is not SelectionHandoffReason:
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_DECISION,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        has_proposal = proposal is not None
        has_reason = reason is not None
        if has_proposal == has_reason:
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_DECISION,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        if proposal is None:
            return None, reason
        if (
            type(proposal.procedure_id) is not str
            or not proposal.procedure_id
            or len(proposal.procedure_id) > PROCEDURE_ID_MAX_LENGTH
            or type(proposal.intent) is not str
            or not proposal.intent
            or len(proposal.intent) > PROCEDURE_INTENT_MAX_LENGTH
            or (proposal.version is not None and not is_valid_procedure_version(proposal.version))
            or (
                proposal.digest is not None
                and (
                    type(proposal.digest) is not str
                    or len(proposal.digest) != 71
                    or not proposal.digest.startswith("sha256:")
                    or any(char not in "0123456789abcdef" for char in proposal.digest[7:])
                )
            )
        ):
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_DECISION,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        canonical_arguments = self._canonicalize_selection_argument_map(
            proposal.arguments,
            trace_id=trace_id,
            request=request,
            classification=classification,
            created_at=created_at,
            events=events,
        )
        return (
            SelectionProposal(
                procedure_id=proposal.procedure_id,
                intent=proposal.intent,
                arguments=canonical_arguments,
                confidence=proposal.confidence,
                version=proposal.version,
                digest=proposal.digest,
            ),
            None,
        )

    def _canonicalize_selection_argument_map(
        self,
        arguments: object,
        *,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> dict[str, str]:
        if type(arguments) is not dict or len(arguments) > PROCEDURE_ARGUMENT_COUNT_MAX:
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_ARGUMENTS,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        canonical: dict[str, str] = {}
        for name, value in arguments.items():
            if (
                type(name) is not str
                or not is_valid_argument_name(name)
                or type(value) is not str
                or len(value) > PROMPT_MAX_LENGTH
            ):
                self._fail_closed_selection(
                    SelectionReasonCode.INVALID_ARGUMENTS,
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )
            canonical[name] = value
        return canonical

    def _validate_selection_arguments(
        self,
        descriptor: ProcedureDescriptor,
        arguments: dict[str, str],
        *,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> dict[str, str]:
        if type(arguments) is not dict:
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_ARGUMENTS,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        expected_arguments = {argument.name: argument for argument in descriptor.arguments}
        if len(arguments) != len(expected_arguments):
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_ARGUMENTS,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        for name, value in arguments.items():
            if type(name) is not str or name not in expected_arguments or type(value) is not str:
                self._fail_closed_selection(
                    SelectionReasonCode.INVALID_ARGUMENTS,
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )
        validated: dict[str, str] = {}
        for name, schema in expected_arguments.items():
            value = arguments[name]
            if schema.value_type is ArgumentValueType.TEXT:
                if not isinstance(value, str):
                    self._fail_closed_selection(
                        SelectionReasonCode.INVALID_ARGUMENTS,
                        trace_id=trace_id,
                        request=request,
                        classification=classification,
                        created_at=created_at,
                        events=events,
                    )
            else:
                self._fail_closed_selection(
                    SelectionReasonCode.INVALID_ARGUMENTS,
                    trace_id=trace_id,
                    request=request,
                    classification=classification,
                    created_at=created_at,
                    events=events,
                )
            if not schema.allow_blank and not value.strip():
                self._fail_closed_selection(
                    SelectionReasonCode.INVALID_ARGUMENTS,
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
                    SelectionReasonCode.INVALID_ARGUMENTS,
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
        value: object,
        *,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
    ) -> float:
        normalized = parse_probability(value)
        if normalized is None:
            self._fail_closed_selection(
                SelectionReasonCode.INVALID_CONFIDENCE,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
            )
        return normalized

    def _enforce_selection_policy(
        self,
        *,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        descriptor: ProcedureDescriptor,
        trace_id: str,
        created_at: datetime,
        events: list[TraceEvent],
        selector_metadata: SelectorMetadata | None = None,
    ) -> None:
        metadata = selector_metadata or self._selector_metadata
        if metadata is None:
            self._fail_closed_selection(
                SelectionReasonCode.NO_SELECTOR,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
                stage="select-policy",
                selector_metadata=metadata,
            )
        if metadata.locality is SelectorLocality.REMOTE:
            self._fail_closed_selection(
                SelectionReasonCode.REMOTE_SELECTION,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
                stage="select-policy",
                selector_metadata=metadata,
            )
        if (
            not descriptor.pure
            or descriptor.effect is not ProcedureEffect.READ_ONLY
            or descriptor.risk is not ProcedureRisk.NONE
            or descriptor.requires_confirmation
        ):
            self._fail_closed_selection(
                SelectionReasonCode.POLICY_DENIED,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
                stage="select-policy",
                selector_metadata=metadata,
            )
        if not descriptor.has_selection_grounding:
            self._fail_closed_selection(
                SelectionReasonCode.GROUNDING_UNAVAILABLE,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
                stage="select-policy",
                selector_metadata=metadata,
            )
        # Raise after the except block so provider grounding failures do not remain
        # reachable through the sanitized UnsupportedWork traceback or context.
        grounding_failed = False
        try:
            grounded = procedure.selection_is_grounded(request, classification)
        except Exception:
            grounding_failed = True
            grounded = False
        if grounding_failed:
            self._fail_closed_selection(
                SelectionReasonCode.GROUNDING_FAILED,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
                stage="select-policy",
                selector_metadata=metadata,
            )
        if type(grounded) is not bool or not grounded:
            self._fail_closed_selection(
                SelectionReasonCode.GROUNDING_FAILED,
                trace_id=trace_id,
                request=request,
                classification=classification,
                created_at=created_at,
                events=events,
                stage="select-policy",
                selector_metadata=metadata,
            )
        events.append(
            TraceEvent(
                "select-policy",
                self._selection_event_detail(SelectionReasonCode.APPROVED, metadata),
            )
        )

    def _fail_closed_selection(
        self,
        reason_code: SelectionReasonCode,
        *,
        trace_id: str,
        request: WorkRequest,
        classification: Classification,
        created_at: datetime,
        events: list[TraceEvent],
        stage: str = "select",
        selector_metadata: SelectorMetadata | None = None,
    ) -> Never:
        events.append(
            TraceEvent(stage, self._selection_event_detail(reason_code, selector_metadata))
        )
        self._record_unsupported_trace(
            trace_id=trace_id,
            request=request,
            classification=classification,
            created_at=created_at,
            events=events,
        )
        raise UnsupportedWork(request.prompt, trace_id)

    def _selection_event_detail(
        self, reason_code: SelectionReasonCode, metadata: SelectorMetadata | None = None
    ) -> str:
        metadata = metadata or self._selector_metadata
        if metadata is None:
            return f"code={reason_code.value}"
        return f"provider={metadata.provider_id} code={reason_code.value}"

    def _normalize_typed_envelope(
        self,
        request: WorkRequest,
        classification: Classification,
        *,
        expected_procedure_id: str | None,
    ) -> tuple[WorkRequest, Classification, str | None]:
        normalized_request = self._normalize_work_request(request, typed_prompt=True)
        if type(classification) is not Classification:
            raise InvalidSelectionProposal("typed classification is invalid")
        if not is_valid_procedure_intent(classification.intent):
            raise InvalidSelectionProposal("typed classification is invalid")
        if not is_valid_classifier_id(classification.classifier):
            raise InvalidSelectionProposal("typed classification is invalid")
        normalized_confidence = parse_probability(classification.confidence)
        if normalized_confidence is None:
            raise InvalidSelectionProposal("typed classification is invalid")
        normalized_arguments = self._normalize_typed_arguments(classification.arguments)
        if expected_procedure_id is not None and not is_valid_procedure_id(expected_procedure_id):
            raise InvalidSelectionProposal("typed classification is invalid")
        return (
            normalized_request,
            Classification(
                intent=classification.intent,
                arguments=normalized_arguments,
                confidence=normalized_confidence,
                classifier=classification.classifier,
            ),
            expected_procedure_id,
        )

    def _normalize_work_request(self, request: object, *, typed_prompt: bool) -> WorkRequest:
        if type(request) is not WorkRequest:
            raise InvalidWorkRequest()
        prompt = (
            self._validate_typed_prompt(request.prompt)
            if typed_prompt
            else normalize_prompt(request.prompt)
        )
        return WorkRequest(
            prompt=prompt,
            attributes=self._normalize_work_request_attributes(request.attributes),
        )

    @staticmethod
    def _validate_typed_prompt(prompt: object) -> str:
        if type(prompt) is not str:
            raise InvalidPrompt("prompt must be a string")
        if not prompt:
            raise InvalidPrompt()
        if len(prompt) > PROMPT_MAX_LENGTH:
            raise InvalidPrompt(f"prompt must be at most {PROMPT_MAX_LENGTH} characters")
        return prompt

    @staticmethod
    def _normalize_work_request_attributes(attributes: object) -> dict[str, object]:
        if type(attributes) is not dict or len(attributes) > WORK_REQUEST_ATTRIBUTE_COUNT_MAX:
            raise InvalidWorkRequest()
        normalized: dict[str, object] = {}
        for name, value in attributes.items():
            if (
                type(name) is not str
                or not name
                or len(name) > WORK_REQUEST_ATTRIBUTE_KEY_MAX_LENGTH
            ):
                raise InvalidWorkRequest()
            if value is None or type(value) is bool:
                normalized[name] = value
                continue
            if type(value) is str:
                if len(value) > PROMPT_MAX_LENGTH:
                    raise InvalidWorkRequest()
                normalized[name] = value
                continue
            if type(value) is int:
                if len(str(value)) > WORK_REQUEST_ATTRIBUTE_NUMBER_MAX_LENGTH:
                    raise InvalidWorkRequest()
                normalized[name] = value
                continue
            if type(value) is float:
                if (
                    not isfinite(value)
                    or len(repr(value)) > WORK_REQUEST_ATTRIBUTE_NUMBER_MAX_LENGTH
                ):
                    raise InvalidWorkRequest()
                normalized[name] = value
                continue
            raise InvalidWorkRequest()
        return normalized

    @staticmethod
    def _normalize_typed_arguments(arguments: object) -> dict[str, str]:
        if type(arguments) is not dict or len(arguments) > PROCEDURE_ARGUMENT_COUNT_MAX:
            raise InvalidSelectionProposal("typed classification is invalid")
        normalized: dict[str, str] = {}
        for name, value in arguments.items():
            if (
                type(name) is not str
                or not is_valid_argument_name(name)
                or type(value) is not str
                or len(value) > PROMPT_MAX_LENGTH
            ):
                raise InvalidSelectionProposal("typed classification is invalid")
            normalized[name] = value
        return normalized

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
