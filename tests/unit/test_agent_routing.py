from __future__ import annotations

import pytest

from af.application.agent_routing import AgentRoutingController, AgentRoutingService
from af.domain.agent_routing import (
    AgentRole,
    AgentRouteReason,
    AgentRouteStatus,
    DeterministicAgentRouter,
)
from af.framework.errors import InvalidPrompt
from af.framework.types import PROMPT_MAX_LENGTH, WorkRequest


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("Design the API architecture", AgentRole.ARCHITECT),
        ("Implement the endpoint", AgentRole.BUILDER),
        ("Review this patch", AgentRole.REVIEWER),
        ("Run tests and coverage", AgentRole.TESTER),
        ("Summarize this discussion", AgentRole.GENERAL),
        ("FIX this defect", AgentRole.BUILDER),
    ],
)
def test_agent_router_selects_one_deterministic_role(prompt: str, expected: AgentRole) -> None:
    result = DeterministicAgentRouter().route(WorkRequest(prompt))

    assert result.status is AgentRouteStatus.ROUTED
    assert result.agent is expected
    assert result.reason in {AgentRouteReason.SPECIALIST_MATCH, AgentRouteReason.GENERAL_FALLBACK}
    assert result.candidates == (expected,)


def test_agent_router_allows_multiple_signals_for_the_same_role() -> None:
    result = DeterministicAgentRouter().route(
        WorkRequest("Plan the requirements and specification")
    )

    assert result.agent is AgentRole.ARCHITECT
    assert result.candidates == (AgentRole.ARCHITECT,)


@pytest.mark.parametrize(
    ("prompt", "expected_candidates"),
    [
        ("Design and implement the endpoint", (AgentRole.ARCHITECT, AgentRole.BUILDER)),
        ("Review the tests", (AgentRole.REVIEWER, AgentRole.TESTER)),
        ("Plan the QA implementation", (AgentRole.ARCHITECT, AgentRole.BUILDER, AgentRole.TESTER)),
    ],
)
def test_agent_router_fails_closed_on_ambiguous_specialist_signals(
    prompt: str,
    expected_candidates: tuple[AgentRole, ...],
) -> None:
    result = DeterministicAgentRouter().route(WorkRequest(prompt))

    assert result.status is AgentRouteStatus.HANDOFF
    assert result.agent is None
    assert result.reason is AgentRouteReason.AMBIGUOUS
    assert result.candidates == expected_candidates


def test_agent_router_matches_whole_words_only() -> None:
    result = DeterministicAgentRouter().route(WorkRequest("Summarize the contest results"))

    assert result.agent is AgentRole.GENERAL
    assert result.reason is AgentRouteReason.GENERAL_FALLBACK


def test_agent_routing_controller_normalizes_before_routing() -> None:
    controller = AgentRoutingController(AgentRoutingService(DeterministicAgentRouter()))

    result = controller.route("  Implement the endpoint  ")

    assert result.prompt == "Implement the endpoint"
    assert result.agent is AgentRole.BUILDER


@pytest.mark.parametrize("prompt", ["   ", "x" * (PROMPT_MAX_LENGTH + 1)])
def test_agent_routing_controller_rejects_invalid_prompts(prompt: str) -> None:
    controller = AgentRoutingController(AgentRoutingService(DeterministicAgentRouter()))

    with pytest.raises(InvalidPrompt):
        controller.route(prompt)


def test_application_composes_agent_routing_controller(application) -> None:  # type: ignore[no-untyped-def]
    result = application.agent_routes.route("Review this patch")

    assert result.agent is AgentRole.REVIEWER
