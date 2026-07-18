from __future__ import annotations

import math
from typing import cast

import pytest

from af.bootstrap import create_application
from af.config import Settings
from af.framework.errors import (
    InvalidProviderConfiguration,
    InvalidRoutingPolicy,
    UnsupportedWork,
    VerificationFailed,
)
from af.framework.routing import OptimisationGoal, RoutingPolicy
from af.framework.types import (
    ProcedureArgumentDescriptor,
    ProcedureDescriptor,
    SelectionDecision,
    SelectionProposal,
    SelectorLocality,
    WorkRequest,
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

    outcome = application.agent.handle(WorkRequest(prompt="count r please"))
    trace = application.traces.get(outcome.trace_id)

    assert selector.prompts == ["count r please"]
    assert selector.catalogues == [
        (
            ProcedureDescriptor(
                procedure_id="hello",
                version="1.0.0",
                intent="system.hello",
                arguments=(),
            ),
            ProcedureDescriptor(
                procedure_id="character-count",
                version="1.0.0",
                intent="text.character_count",
                arguments=(
                    ProcedureArgumentDescriptor(
                        name="text",
                        value_type=str,
                        min_length=1,
                        max_length=10_000,
                        allow_blank=False,
                    ),
                    ProcedureArgumentDescriptor(
                        name="character",
                        value_type=str,
                        min_length=1,
                        max_length=1,
                        allow_blank=True,
                    ),
                ),
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

    outcome = application.agent.handle(WorkRequest(prompt="count r please"))
    trace = application.traces.get(outcome.trace_id)

    assert outcome.output == "3"
    assert any(event.stage == "select" and "remote" in event.detail for event in trace.events)


def test_unknown_work_fails_when_no_selector_is_installed(application) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(UnsupportedWork):
        application.agent.handle(WorkRequest(prompt="invent a new universe"))


@pytest.mark.parametrize(
    ("decision", "expected_reason"),
    [
        (
            SelectionDecision(reason="ambiguous between registered procedures"),
            "ambiguous",
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
            "confidence",
        ),
        (
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="missing",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=0.95,
                )
            ),
            "unknown procedure",
        ),
        (
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="hello",
                    intent="system.goodbye",
                    arguments={},
                    confidence=0.95,
                )
            ),
            "unsupported intent",
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
            "invalid arguments",
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


@pytest.mark.parametrize(
    "confidence",
    [math.nan, math.inf, -math.inf, -0.01, 1.01],
)
def test_invalid_selector_confidence_fails_closed_without_execution(confidence: float) -> None:
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
        event.stage == "select" and "invalid confidence" in event.detail for event in trace.events
    )


@pytest.mark.parametrize(
    "threshold",
    [math.nan, math.inf, -math.inf, -0.01, 1.01],
)
def test_invalid_selector_threshold_fails_closed_without_execution(threshold: float) -> None:
    application = create_application(
        settings=Settings(selector_confidence_threshold=threshold),
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
        application.agent.handle(WorkRequest(prompt="route this somehow"))

    trace = application.traces.get(captured.value.trace_id)

    assert trace.status == "unsupported"
    assert trace.route is None
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select" and "invalid selector threshold" in event.detail
        for event in trace.events
    )


@pytest.mark.parametrize(
    ("decision", "expected_reason"),
    [
        (SelectionDecision(), "invalid selector decision"),
        (
            SelectionDecision(
                proposal=SelectionProposal(
                    procedure_id="character-count",
                    intent="text.character_count",
                    arguments={"text": "strawberry", "character": "r"},
                    confidence=0.95,
                ),
                reason="ambiguous",
            ),
            "invalid selector decision",
        ),
        (SelectionDecision(reason="   "), "invalid selector decision"),
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
        event.stage == "select" and "invalid arguments" in event.detail for event in trace.events
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
    assert all(event.stage != "execute" for event in trace.events)
    assert any(
        event.stage == "select"
        and "selector failure" in event.detail
        and "secret-token" not in event.detail
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
