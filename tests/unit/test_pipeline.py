from __future__ import annotations

import math
import traceback
from collections.abc import Iterator
from typing import Any, cast

import pytest

from af.bootstrap import create_application
from af.config import Settings
from af.domain.procedures import BaseProcedure
from af.framework.errors import (
    InvalidPrompt,
    InvalidProviderConfiguration,
    InvalidRoutingPolicy,
    InvalidSelectionProposal,
    UnsupportedWork,
    VerificationFailed,
)
from af.framework.routing import OptimisationGoal, ProcedureRegistry, RoutingPolicy
from af.framework.types import (
    PROCEDURE_ARGUMENT_COUNT_MAX,
    PROMPT_MAX_LENGTH,
    ArgumentValueType,
    Classification,
    ProcedureArgumentDescriptor,
    ProcedureDescriptor,
    ProcedureEffect,
    ProcedureResult,
    ProcedureRisk,
    RouteMetrics,
    SelectionDecision,
    SelectionHandoffReason,
    SelectionProposal,
    SelectionReasonCode,
    SelectorLocality,
    WorkRequest,
    WorkTier,
)
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository


def test_pipeline_runs_af_stages_in_order(application) -> None:  # type: ignore[no-untyped-def]
    outcome = application.agent.handle(WorkRequest(prompt="how many rs in strawberry"))
    trace = application.traces.get(outcome.trace_id)

    assert outcome.output == "3"
    assert outcome.verified is True
    assert outcome.route == "character-count"
    assert [event.stage for event in trace.events] == [
        "classify",
        "route",
        "execute",
        "verify",
        "record",
        "test",
        "reuse",
    ]
    assert trace.regression_passed is True
    assert trace.promoted is True


def test_second_identical_request_reuses_but_still_verifies(application) -> None:  # type: ignore[no-untyped-def]
    first = application.agent.handle(WorkRequest(prompt="how many rs in strawberry"))
    second = application.agent.handle(WorkRequest(prompt="how many rs in strawberry"))

    assert first.reused is False
    assert second.reused is True
    assert second.verified is True
    assert application.traces.get(second.trace_id).reused is True


class ExplodingSelector:
    @property
    def name(self) -> str:
        return "exploding-test-selector"

    @property
    def locality(self) -> SelectorLocality:
        return SelectorLocality.LOCAL

    def select(
        self,
        prompt: str,
        catalogue: tuple[ProcedureDescriptor, ...],
    ) -> SelectionDecision:
        raise AssertionError(
            f"selector must not receive deterministic work: {prompt} / {catalogue!r}"
        )


def test_deterministic_work_never_invokes_installed_selector() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=ExplodingSelector(),
    )

    assert application.agent.handle(WorkRequest(prompt="how many rs in strawberry")).output == "3"
    assert application.agent.handle(WorkRequest(prompt="hello")).output == "Hello, world!"


class RecordingSelector:
    def __init__(
        self,
        decision: SelectionDecision,
        *,
        locality: SelectorLocality = SelectorLocality.LOCAL,
    ) -> None:
        self._decision = decision
        self._locality = locality
        self.prompts: list[str] = []
        self.catalogues: list[tuple[ProcedureDescriptor, ...]] = []

    @property
    def name(self) -> str:
        return "recording-test-selector"

    @property
    def locality(self) -> SelectorLocality:
        return self._locality

    def select(
        self,
        prompt: str,
        catalogue: tuple[ProcedureDescriptor, ...],
    ) -> SelectionDecision:
        self.prompts.append(prompt)
        self.catalogues.append(catalogue)
        return self._decision


class FailingSelector:
    @property
    def name(self) -> str:
        return "failing-test-selector"

    @property
    def locality(self) -> SelectorLocality:
        return SelectorLocality.LOCAL

    def select(
        self,
        prompt: str,
        catalogue: tuple[ProcedureDescriptor, ...],
    ) -> SelectionDecision:
        raise RuntimeError(f"provider exploded for {prompt}: secret-token")


class StringRemoteSelector(RecordingSelector):
    @property
    def locality(self) -> SelectorLocality:
        return cast(SelectorLocality, "remote")


class MutableLocalitySelector(RecordingSelector):
    @property
    def locality(self) -> SelectorLocality:
        return self._locality

    def switch_to_remote(self) -> None:
        self._locality = cast(SelectorLocality, SelectorLocality.REMOTE)


class ExplodingNameSelector(RecordingSelector):
    @property
    def name(self) -> str:
        raise RuntimeError("SECRET-name")


class NamedSelector(RecordingSelector):
    def __init__(self, name: str, decision: SelectionDecision) -> None:
        super().__init__(decision)
        self._name = name

    @property
    def name(self) -> str:
        return self._name


class ExplodingLocalitySelector(RecordingSelector):
    @property
    def locality(self) -> SelectorLocality:
        raise RuntimeError("SECRET-locality")


class SideEffectProcedure(BaseProcedure):
    id = "side-effect"
    tier = WorkTier.PROCEDURAL
    pure = False
    replay_safe = False
    metrics = RouteMetrics(cost=5, latency=5, intelligence=1)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=BaseProcedure.version,
        intent="side.effect",
        arguments=(),
        effect=ProcedureEffect.SIDE_EFFECT,
        risk=ProcedureRisk.ELEVATED,
        requires_confirmation=True,
    )

    def __init__(self) -> None:
        self.execute_calls = 0

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "side.effect"

    def execute(self, classification: Classification) -> ProcedureResult:
        del classification
        self.execute_calls += 1
        return ProcedureResult(output="done")

    def validate(self, classification: Classification) -> None:
        if classification.intent != "side.effect" or classification.arguments:
            raise InvalidSelectionProposal("invalid arguments for side-effect")


class GroundingFailureProcedure(BaseProcedure):
    id = "grounding-failure"
    tier = WorkTier.PROCEDURAL
    pure = True
    replay_safe = True
    has_selection_grounding = True
    metrics = RouteMetrics(cost=1, latency=1, intelligence=1)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=BaseProcedure.version,
        intent="grounding.failure",
        arguments=(),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
        requires_confirmation=False,
    )

    def __init__(self) -> None:
        self.execute_calls = 0

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "grounding.failure"

    def execute(self, classification: Classification) -> ProcedureResult:
        del classification
        self.execute_calls += 1
        return ProcedureResult(output="done")

    def validate(self, classification: Classification) -> None:
        if classification.intent != "grounding.failure" or classification.arguments:
            raise InvalidSelectionProposal("invalid arguments for grounding-failure")

    def selection_is_grounded(
        self,
        request: WorkRequest,
        classification: Classification,
    ) -> bool:
        del request, classification
        raise RuntimeError("SECRET-grounding")


class SecretValidationProcedure(BaseProcedure):
    id = "secret-validation"
    tier = WorkTier.PROCEDURAL
    pure = True
    replay_safe = True
    has_selection_grounding = True
    metrics = RouteMetrics(cost=1, latency=1, intelligence=1)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=BaseProcedure.version,
        intent="secret.validation",
        arguments=(),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
        requires_confirmation=False,
    )

    def __init__(self) -> None:
        self.execute_calls = 0

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "secret.validation"

    def execute(self, classification: Classification) -> ProcedureResult:
        del classification
        self.execute_calls += 1
        return ProcedureResult(output="done")

    def validate(self, classification: Classification) -> None:
        del classification
        raise InvalidSelectionProposal("SECRET-validation")

    def selection_is_grounded(
        self,
        request: WorkRequest,
        classification: Classification,
    ) -> bool:
        del request, classification
        return True


class SnapshotMutableProcedure(BaseProcedure):
    id = "snapshot-mutable"
    tier = WorkTier.PROCEDURAL
    pure = True
    replay_safe = True
    has_selection_grounding = False
    metrics = RouteMetrics(cost=1, latency=1, intelligence=1)
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=BaseProcedure.version,
        intent="snapshot.mutable",
        arguments=(),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
        requires_confirmation=False,
    )

    def __init__(self, *, grounding_result: object = False) -> None:
        self.execute_calls = 0
        self._grounding_result = grounding_result

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "snapshot.mutable"

    def execute(self, classification: Classification) -> ProcedureResult:
        del classification
        self.execute_calls += 1
        return ProcedureResult(output="done")

    def validate(self, classification: Classification) -> None:
        if classification.intent != "snapshot.mutable" or classification.arguments:
            raise InvalidSelectionProposal("invalid arguments for snapshot-mutable")

    def selection_is_grounded(
        self,
        request: WorkRequest,
        classification: Classification,
    ) -> bool:
        del request, classification
        return cast(bool, self._grounding_result)


class MalformedDescriptorProcedure(BaseProcedure):
    tier = WorkTier.PROCEDURAL
    pure = True
    replay_safe = True
    metrics = RouteMetrics(cost=1, latency=1, intelligence=1)

    def __init__(self, *, procedure_id: str, descriptor: ProcedureDescriptor) -> None:
        self.id = procedure_id
        self.selection_descriptor = descriptor

    def supports(self, classification: Classification) -> bool:
        return classification.intent == self.selection_descriptor.intent

    def execute(self, classification: Classification) -> ProcedureResult:
        del classification
        return ProcedureResult(output="ok")

    def validate(self, classification: Classification) -> None:
        if classification.intent != self.selection_descriptor.intent:
            raise InvalidSelectionProposal("invalid intent")


def test_local_selector_can_route_unknown_work_through_registered_procedure() -> None:
    selector = RecordingSelector(
        SelectionDecision(
            proposal=SelectionProposal(
                procedure_id="character-count",
                intent="text.character_count",
                arguments={"text": "strawberry", "character": "r"},
                confidence=0.95,
            )
        )
    )
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )

    outcome = application.agent.handle(
        WorkRequest(prompt="please count the letter r in strawberry")
    )
    trace = application.traces.get(outcome.trace_id)

    assert selector.prompts == ["please count the letter r in strawberry"]
    assert selector.catalogues == [
        (
            ProcedureDescriptor(
                procedure_id="hello",
                version="1.0.0",
                intent="system.hello",
                arguments=(),
                effect=ProcedureEffect.READ_ONLY,
                risk=ProcedureRisk.NONE,
                requires_confirmation=False,
                tier=WorkTier.STATIC,
                pure=True,
                replay_safe=True,
                has_selection_grounding=False,
            ),
            ProcedureDescriptor(
                procedure_id="character-count",
                version="1.0.0",
                intent="text.character_count",
                arguments=(
                    ProcedureArgumentDescriptor(
                        name="text",
                        value_type=ArgumentValueType.TEXT,
                        min_length=1,
                        max_length=10_000,
                        allow_blank=False,
                    ),
                    ProcedureArgumentDescriptor(
                        name="character",
                        value_type=ArgumentValueType.TEXT,
                        min_length=1,
                        max_length=1,
                        allow_blank=True,
                    ),
                ),
                effect=ProcedureEffect.READ_ONLY,
                risk=ProcedureRisk.NONE,
                requires_confirmation=False,
                tier=WorkTier.PROCEDURAL,
                pure=True,
                replay_safe=True,
                has_selection_grounding=True,
            ),
        )
    ]
    assert outcome.route == "character-count"
    assert outcome.tier == "procedural"
    assert outcome.verified is True
    assert outcome.reused is False
    assert trace.regression_passed is True
    assert trace.promoted is True
    assert any(
        event.stage == "select"
        and "recording-test-selector" in event.detail
        and "local" in event.detail
        and "0.95" in event.detail
        for event in trace.events
    )
    assert any(
        event.stage == "select-policy" and "approved" in event.detail for event in trace.events
    )


def test_remote_selector_requires_explicit_opt_in() -> None:
    selector = RecordingSelector(
        SelectionDecision(
            proposal=SelectionProposal(
                procedure_id="character-count",
                intent="text.character_count",
                arguments={"text": "strawberry", "character": "r"},
                confidence=0.95,
            )
        ),
        locality=SelectorLocality.REMOTE,
    )

    with pytest.raises(InvalidProviderConfiguration):
        create_application(
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
            selector=selector,
        )

    application = create_application(
        settings=Settings(allow_remote_inference=True),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="please count the letter r in strawberry"))

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert trace.route is None
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select-policy"
        and SelectionReasonCode.REMOTE_SELECTION.value in event.detail
        for event in trace.events
    )


def test_unknown_work_fails_when_no_selector_is_installed(application) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(UnsupportedWork):
        application.agent.handle(WorkRequest(prompt="invent a new universe"))


def test_string_locality_selector_is_rejected_at_composition() -> None:
    with pytest.raises(InvalidProviderConfiguration, match="selector provider metadata is invalid"):
        create_application(
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
            selector=StringRemoteSelector(
                SelectionDecision(
                    proposal=SelectionProposal(
                        procedure_id="character-count",
                        intent="text.character_count",
                        arguments={"text": "strawberry", "character": "r"},
                        confidence=0.95,
                    )
                )
            ),
        )


def test_selector_metadata_is_snapshotted_once() -> None:
    selector = MutableLocalitySelector(
        SelectionDecision(
            proposal=SelectionProposal(
                procedure_id="character-count",
                intent="text.character_count",
                arguments={"text": "strawberry", "character": "r"},
                confidence=0.95,
            )
        )
    )
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )
    selector.switch_to_remote()

    outcome = application.agent.handle(
        WorkRequest(prompt="please count the letter r in strawberry")
    )
    trace = application.traces.get(outcome.trace_id)

    assert outcome.output == "3"
    assert any(
        event.stage == "select"
        and "locality=local" in event.detail
        and "locality=remote" not in event.detail
        for event in trace.events
    )


@pytest.mark.parametrize(
    "selector",
    [
        ExplodingNameSelector(
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=0.95,
                )
            )
        ),
        ExplodingLocalitySelector(
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=0.95,
                )
            )
        ),
    ],
)
def test_selector_metadata_property_failures_are_sanitized(selector: RecordingSelector) -> None:
    with pytest.raises(
        InvalidProviderConfiguration,
        match="selector provider metadata is invalid",
    ) as captured:
        create_application(
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
            selector=selector,
        )

    assert "SECRET" not in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "SECRET" not in "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )


@pytest.mark.parametrize(
    "name",
    ["SECRET-provider", "remote://selector", "bad\nselector", "x" * 65],
)
def test_selector_provider_id_must_be_bounded_and_opaque(name: str) -> None:
    with pytest.raises(InvalidProviderConfiguration, match="selector provider metadata is invalid"):
        create_application(
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
            selector=NamedSelector(
                name,
                SelectionDecision(
                    proposal=SelectionProposal(
                        procedure_id="character-count",
                        intent="text.character_count",
                        arguments={"text": "strawberry", "character": "r"},
                        confidence=0.95,
                    )
                ),
            ),
        )


@pytest.mark.parametrize(
    ("decision", "expected_reason"),
    [
        (
            SelectionDecision(reason=SelectionHandoffReason.AMBIGUOUS),
            SelectionReasonCode.AMBIGUOUS.value,
        ),
        (
            SelectionDecision(reason=SelectionHandoffReason.UNAVAILABLE),
            SelectionReasonCode.UNAVAILABLE.value,
        ),
        (
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=0.49,
                )
            ),
            SelectionReasonCode.BELOW_THRESHOLD.value,
        ),
        (
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="SECRET-missing",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=0.95,
                )
            ),
            SelectionReasonCode.UNKNOWN_PROCEDURE.value,
        ),
        (
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="hello",
                    intent="SECRET.goodbye",
                    arguments={},
                    confidence=0.95,
                )
            ),
            SelectionReasonCode.UNSUPPORTED_INTENT.value,
        ),
        (
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "rr"},
                    confidence=0.95,
                )
            ),
            SelectionReasonCode.INVALID_ARGUMENTS.value,
        ),
    ],
)
def test_invalid_selector_proposals_fail_closed_without_execution(
    decision: SelectionDecision,
    expected_reason: str,
) -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=RecordingSelector(decision),
    )

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="route this somehow"))

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert trace.route is None
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select" and expected_reason in event.detail for event in trace.events
    )
    assert "SECRET" not in " ".join(event.detail for event in trace.events)


@pytest.mark.parametrize(
    "confidence",
    [True, math.nan, math.inf, -math.inf, -0.01, 1.01, 10**400],
)
def test_invalid_selector_confidence_fails_closed_without_execution(confidence: object) -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=RecordingSelector(
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=confidence,
                )
            )
        ),
    )

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="route this somehow"))

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert trace.route is None
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select" and SelectionReasonCode.INVALID_CONFIDENCE.value in event.detail
        for event in trace.events
    )


@pytest.mark.parametrize(
    "threshold",
    [True, math.nan, math.inf, -math.inf, -0.01, 1.01, 10**400],
)
def test_invalid_selector_threshold_is_rejected_at_composition(threshold: object) -> None:
    with pytest.raises(
        InvalidProviderConfiguration,
        match="selector confidence threshold must be a finite probability in \\[0, 1\\]",
    ):
        create_application(
            settings=Settings(selector_confidence_threshold=cast(float, threshold)),
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
            selector=RecordingSelector(
                SelectionDecision(
                    proposal=SelectionProposal(
                        procedure_id="character-count",
                        intent="text.character_count",
                        arguments={"text": "strawberry", "character": "r"},
                        confidence=0.95,
                    )
                )
            ),
        )


@pytest.mark.parametrize(
    ("decision", "expected_reason"),
    [
        (SelectionDecision(), SelectionReasonCode.INVALID_DECISION.value),
        (
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=0.95,
                ),
                reason=SelectionHandoffReason.AMBIGUOUS,
            ),
            SelectionReasonCode.INVALID_DECISION.value,
        ),
        (
            cast(SelectionDecision, SelectionDecision(reason=cast(Any, "SECRET-ambiguous"))),
            SelectionReasonCode.INVALID_DECISION.value,
        ),
    ],
)
def test_malformed_selector_decisions_fail_closed_without_execution(
    decision: SelectionDecision,
    expected_reason: str,
) -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=RecordingSelector(decision),
    )

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="route this somehow"))

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert trace.route is None
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select" and expected_reason in event.detail for event in trace.events
    )
    assert "SECRET" not in " ".join(event.detail for event in trace.events)


@pytest.mark.parametrize(
    "arguments",
    [
        cast(dict[str, str], {"text": "strawberry"}),
        cast(dict[str, str], {"text": "strawberry", "character": "r", "extra": "x"}),
        cast(dict[str, str], {"text": "strawberry", "character": None}),
        {"text": "   ", "character": "r"},
        {"text": "a" * 10_001, "character": "r"},
        {"text": "strawberry", "character": ""},
        {"text": "strawberry", "character": "rr"},
    ],
)
def test_selector_argument_validation_fails_closed_without_execution(
    arguments: dict[str, str],
) -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=RecordingSelector(
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments=arguments,
                    confidence=0.95,
                )
            )
        ),
    )

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="route this somehow"))

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert trace.route is None
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select" and SelectionReasonCode.INVALID_ARGUMENTS.value in event.detail
        for event in trace.events
    )


class OversizedArgumentMap(dict[str, str]):
    def __iter__(self) -> Iterator[str]:
        raise AssertionError("selection arguments should fail closed before iteration")


def test_selector_argument_count_is_bounded_before_iteration() -> None:
    arguments = OversizedArgumentMap({"text": "strawberry", "character": "r", "extra": "boom"})
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=RecordingSelector(
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments=arguments,
                    confidence=0.95,
                )
            )
        ),
    )

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="route this somehow"))

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select" and SelectionReasonCode.INVALID_ARGUMENTS.value in event.detail
        for event in trace.events
    )


def test_selector_failures_are_sanitized_and_fail_closed() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=FailingSelector(),
    )

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="route this somehow"))

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert trace.route is None
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select"
        and SelectionReasonCode.SELECTOR_FAILURE.value in event.detail
        and "secret-token" not in event.detail
        for event in trace.events
    )
    assert "SECRET" not in "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )


def test_selector_misroute_fails_grounding_policy_without_execution() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=RecordingSelector(
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=0.95,
                )
            )
        ),
    )

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(
            WorkRequest(prompt="Please calculate the escape velocity of Earth")
        )

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert trace.route is None
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select-policy"
        and SelectionReasonCode.GROUNDING_FAILED.value in event.detail
        for event in trace.events
    )


def test_selected_side_effect_procedure_is_denied_before_execution() -> None:
    selector = RecordingSelector(
        SelectionDecision(
            proposal=SelectionProposal(
                procedure_id="side-effect",
                intent="side.effect",
                arguments={},
                confidence=0.95,
            )
        )
    )
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )
    procedure = SideEffectProcedure()
    application.container.resolve(ProcedureRegistry).register(procedure)

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="perform the side effect"))

    trace = application.traces.get(captured.value.trace_id)

    assert procedure.execute_calls == 0
    assert trace.status == "unsupported"
    assert trace.route is None
    assert any(
        event.stage == "select-policy" and SelectionReasonCode.POLICY_DENIED.value in event.detail
        for event in trace.events
    )


def test_grounding_hook_failures_are_sanitized_and_fail_closed() -> None:
    selector = RecordingSelector(
        SelectionDecision(
            proposal=SelectionProposal(
                procedure_id="grounding-failure",
                intent="grounding.failure",
                arguments={},
                confidence=0.95,
            )
        )
    )
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )
    procedure = GroundingFailureProcedure()
    application.container.resolve(ProcedureRegistry).register(procedure)

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="trigger the grounding path"))

    trace = application.traces.get(captured.value.trace_id)

    assert procedure.execute_calls == 0
    assert trace.status == "unsupported"
    assert trace.route is None
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert any(
        event.stage == "select-policy"
        and SelectionReasonCode.GROUNDING_FAILED.value in event.detail
        and "SECRET" not in event.detail
        for event in trace.events
    )
    assert "SECRET" not in "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )


def test_selected_procedure_validation_failures_are_sanitized_and_fail_closed() -> None:
    selector = RecordingSelector(
        SelectionDecision(
            proposal=SelectionProposal(
                procedure_id="secret-validation",
                intent="secret.validation",
                arguments={},
                confidence=0.95,
            )
        )
    )
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )
    procedure = SecretValidationProcedure()
    application.container.resolve(ProcedureRegistry).register(procedure)

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="trigger the secret validation path"))

    trace = application.traces.get(captured.value.trace_id)

    assert procedure.execute_calls == 0
    assert trace.status == "unsupported"
    assert trace.route is None
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert any(
        event.stage == "select"
        and SelectionReasonCode.INVALID_ARGUMENTS.value in event.detail
        and "SECRET" not in event.detail
        for event in trace.events
    )
    assert "SECRET" not in "".join(
        traceback.format_exception(
            type(captured.value),
            captured.value,
            captured.value.__traceback__,
        )
    )


@pytest.mark.parametrize(
    "prompt",
    [cast(str, None), "", "   ", "a" * 10_001],
)
def test_agent_public_boundary_rejects_invalid_prompts_without_tracing(prompt: str) -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    with pytest.raises(InvalidPrompt):
        application.agent.handle(WorkRequest(prompt=prompt))

    assert application.traces.list() == []


@pytest.mark.parametrize(
    ("procedure_id", "descriptor", "message"),
    [
        (
            "character-count",
            ProcedureDescriptor(
                procedure_id="different-id",
                version="1.0.0",
                intent="text.character_count",
                arguments=(),
                effect=ProcedureEffect.READ_ONLY,
                risk=ProcedureRisk.NONE,
                requires_confirmation=False,
            ),
            "must match the registered procedure id",
        ),
        (
            "character-count",
            ProcedureDescriptor(
                procedure_id="character-count",
                version="1.0.0",
                intent="text.character_count",
                arguments=(
                    ProcedureArgumentDescriptor(
                        name="text",
                        value_type=ArgumentValueType.TEXT,
                        min_length=1,
                        max_length=10_000,
                        allow_blank=False,
                    ),
                    ProcedureArgumentDescriptor(
                        name="text",
                        value_type=ArgumentValueType.TEXT,
                        min_length=1,
                        max_length=1,
                        allow_blank=False,
                    ),
                ),
                effect=ProcedureEffect.READ_ONLY,
                risk=ProcedureRisk.NONE,
                requires_confirmation=False,
            ),
            "argument names must be unique",
        ),
        (
            "character-count",
            cast(
                ProcedureDescriptor,
                ProcedureDescriptor(
                    procedure_id="character-count",
                    version="1.0.0",
                    intent="text.character_count",
                    arguments=(
                        cast(
                            ProcedureArgumentDescriptor,
                            ProcedureArgumentDescriptor(
                                name="text",
                                value_type=cast(ArgumentValueType, "python.type"),
                                min_length=1,
                                max_length=10_000,
                                allow_blank=False,
                            ),
                        ),
                    ),
                    effect=ProcedureEffect.READ_ONLY,
                    risk=ProcedureRisk.NONE,
                    requires_confirmation=False,
                ),
            ),
            "invalid argument type",
        ),
        (
            "character-count",
            cast(
                ProcedureDescriptor,
                ProcedureDescriptor(
                    procedure_id="character-count",
                    version="1.0.0",
                    intent="text.character_count",
                    arguments=cast(tuple[ProcedureArgumentDescriptor, ...], []),
                    effect=ProcedureEffect.READ_ONLY,
                    risk=ProcedureRisk.NONE,
                    requires_confirmation=False,
                ),
            ),
            "arguments must be a tuple",
        ),
        (
            "character-count",
            cast(
                ProcedureDescriptor,
                ProcedureDescriptor(
                    procedure_id="character-count",
                    version="1.0.0",
                    intent="text.character_count",
                    arguments=(
                        cast(
                            ProcedureArgumentDescriptor,
                            ProcedureArgumentDescriptor(
                                name="text",
                                value_type=ArgumentValueType.TEXT,
                                min_length=cast(int, True),
                                max_length=PROMPT_MAX_LENGTH,
                                allow_blank=False,
                            ),
                        ),
                    ),
                    effect=ProcedureEffect.READ_ONLY,
                    risk=ProcedureRisk.NONE,
                    requires_confirmation=False,
                ),
            ),
            "invalid minimum length",
        ),
        (
            "character-count",
            cast(
                ProcedureDescriptor,
                ProcedureDescriptor(
                    procedure_id="character-count",
                    version="1.0.0",
                    intent="text.character_count",
                    arguments=(
                        cast(
                            ProcedureArgumentDescriptor,
                            ProcedureArgumentDescriptor(
                                name="text",
                                value_type=ArgumentValueType.TEXT,
                                min_length=1,
                                max_length=cast(int, True),
                                allow_blank=False,
                            ),
                        ),
                    ),
                    effect=ProcedureEffect.READ_ONLY,
                    risk=ProcedureRisk.NONE,
                    requires_confirmation=False,
                ),
            ),
            "invalid maximum length",
        ),
        (
            "character-count",
            cast(
                ProcedureDescriptor,
                ProcedureDescriptor(
                    procedure_id="character-count",
                    version="1.0.0",
                    intent="text.character_count",
                    arguments=(
                        cast(
                            ProcedureArgumentDescriptor,
                            ProcedureArgumentDescriptor(
                                name="text",
                                value_type=ArgumentValueType.TEXT,
                                min_length=1,
                                max_length=PROMPT_MAX_LENGTH + 1,
                                allow_blank=False,
                            ),
                        ),
                    ),
                    effect=ProcedureEffect.READ_ONLY,
                    risk=ProcedureRisk.NONE,
                    requires_confirmation=False,
                ),
            ),
            "invalid maximum length",
        ),
        (
            "character-count",
            cast(
                ProcedureDescriptor,
                ProcedureDescriptor(
                    procedure_id="character-count",
                    version="1.0.0",
                    intent="text.character_count",
                    arguments=(
                        cast(
                            ProcedureArgumentDescriptor,
                            ProcedureArgumentDescriptor(
                                name="text",
                                value_type=ArgumentValueType.TEXT,
                                min_length=1,
                                max_length=PROMPT_MAX_LENGTH,
                                allow_blank=cast(bool, 1),
                            ),
                        ),
                    ),
                    effect=ProcedureEffect.READ_ONLY,
                    risk=ProcedureRisk.NONE,
                    requires_confirmation=False,
                ),
            ),
            "invalid blank policy",
        ),
        (
            "character-count",
            ProcedureDescriptor(
                procedure_id="character-count",
                version="1.0.0",
                intent="text.character_count",
                arguments=tuple(
                    ProcedureArgumentDescriptor(
                        name=f"arg{index}",
                        value_type=ArgumentValueType.TEXT,
                        min_length=0,
                        max_length=1,
                        allow_blank=True,
                    )
                    for index in range(PROCEDURE_ARGUMENT_COUNT_MAX + 1)
                ),
                effect=ProcedureEffect.READ_ONLY,
                risk=ProcedureRisk.NONE,
                requires_confirmation=False,
            ),
            "too many argument descriptors",
        ),
    ],
)
def test_registry_rejects_malformed_selection_descriptors(
    procedure_id: str,
    descriptor: ProcedureDescriptor,
    message: str,
) -> None:
    registry = ProcedureRegistry()

    with pytest.raises(ValueError, match=message):
        registry.register(
            MalformedDescriptorProcedure(procedure_id=procedure_id, descriptor=descriptor)
        )


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("tier", cast(WorkTier, 2), "invalid execution tier"),
        ("pure", cast(bool, 1), "invalid purity policy"),
        ("replay_safe", cast(bool, 1), "invalid replay safety policy"),
        ("has_selection_grounding", cast(bool, 1), "invalid grounding policy"),
    ],
)
def test_registry_rejects_invalid_procedure_safety_fields(
    attribute: str,
    value: object,
    message: str,
) -> None:
    procedure = SnapshotMutableProcedure()
    setattr(procedure, attribute, value)
    registry = ProcedureRegistry()

    with pytest.raises(ValueError, match=message):
        registry.register(procedure)


def test_selection_policy_uses_registered_grounding_snapshot() -> None:
    selector = RecordingSelector(
        SelectionDecision(
            proposal=SelectionProposal(
                procedure_id="snapshot-mutable",
                intent="snapshot.mutable",
                arguments={},
                confidence=0.95,
            )
        )
    )
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )
    procedure = SnapshotMutableProcedure(grounding_result=True)
    application.container.resolve(ProcedureRegistry).register(procedure)
    procedure.has_selection_grounding = True

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="trigger the snapshot grounding path"))

    trace = application.traces.get(captured.value.trace_id)

    assert procedure.execute_calls == 0
    assert trace.status == "unsupported"
    assert any(
        event.stage == "select-policy"
        and SelectionReasonCode.GROUNDING_UNAVAILABLE.value in event.detail
        for event in trace.events
    )


def test_non_boolean_grounding_result_fails_closed_without_execution() -> None:
    selector = RecordingSelector(
        SelectionDecision(
            proposal=SelectionProposal(
                procedure_id="snapshot-mutable",
                intent="snapshot.mutable",
                arguments={},
                confidence=0.95,
            )
        )
    )
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        selector=selector,
    )
    procedure = SnapshotMutableProcedure(grounding_result=1)
    procedure.has_selection_grounding = True
    application.container.resolve(ProcedureRegistry).register(procedure)

    with pytest.raises(UnsupportedWork) as captured:
        application.agent.handle(WorkRequest(prompt="trigger the non-boolean grounding path"))

    trace = application.traces.get(captured.value.trace_id)

    assert procedure.execute_calls == 0
    assert trace.status == "unsupported"
    assert any(
        event.stage == "select-policy"
        and SelectionReasonCode.GROUNDING_FAILED.value in event.detail
        for event in trace.events
    )


def test_routing_policy_requires_one_or_two_explicit_goals() -> None:
    with pytest.raises(InvalidRoutingPolicy):
        RoutingPolicy(
            goals=(
                OptimisationGoal.CHEAPEST,
                OptimisationGoal.FASTEST,
                OptimisationGoal.SMARTEST,
            )
        )


def test_verification_failure_is_recorded_and_fails_closed(application) -> None:  # type: ignore[no-untyped-def]
    application.verifier.force_failure = True

    with pytest.raises(VerificationFailed) as captured:
        application.agent.handle(WorkRequest(prompt="how many rs in strawberry"))

    trace = application.traces.get(captured.value.trace_id)
    assert trace.verified is False
    assert trace.promoted is False
