from __future__ import annotations

import pytest

from af.bootstrap import create_application
from af.framework.contracts import Procedure
from af.framework.errors import (
    InvalidProviderConfiguration,
    InvalidRoutingPolicy,
    UnsupportedWork,
    VerificationFailed,
)
from af.framework.routing import OptimisationGoal, RoutingPolicy
from af.framework.types import (
    Classification,
    ProcedureResult,
    VerificationResult,
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


class ExplodingModel:
    @property
    def name(self) -> str:
        return "exploding-test-model"

    def complete(self, prompt: str) -> str:
        raise AssertionError(f"model must not receive deterministic work: {prompt}")


class AcceptingModelVerifier:
    def verify(
        self,
        request: WorkRequest,
        classification: Classification,
        procedure: Procedure,
        result: ProcedureResult,
    ) -> VerificationResult:
        del request, classification, procedure
        passed = bool(result.output.strip())
        return VerificationResult(passed, "model output accepted" if passed else "empty output")


def test_deterministic_work_never_invokes_installed_model() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        model_provider=ExplodingModel(),
        model_verifier=AcceptingModelVerifier(),
    )

    assert application.agent.handle(WorkRequest(prompt="how many rs in strawberry")).output == "3"


class RecordingModel:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    @property
    def name(self) -> str:
        return "recording-test-model"

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "A model-generated but explicitly verified answer"


def test_explicit_model_route_requires_and_uses_its_verifier() -> None:
    model = RecordingModel()
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        model_provider=model,
        model_verifier=AcceptingModelVerifier(),
    )

    outcome = application.agent.handle(WorkRequest(prompt="invent a new universe"))
    trace = application.traces.get(outcome.trace_id)

    assert model.prompts == ["invent a new universe"]
    assert outcome.route == "model-provider"
    assert outcome.tier == "generative"
    assert outcome.verified is True
    assert outcome.reused is False
    assert trace.regression_passed is False
    assert trace.promoted is False


def test_model_and_verifier_must_be_installed_together() -> None:
    with pytest.raises(InvalidProviderConfiguration):
        create_application(
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
            model_provider=RecordingModel(),
        )


def test_unknown_work_fails_when_no_generative_provider_is_installed(application) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(UnsupportedWork):
        application.agent.handle(WorkRequest(prompt="invent a new universe"))


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
