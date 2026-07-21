"""Deterministic routing from natural-language work to installed agent roles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from af.framework.types import WorkRequest


class AgentRole(StrEnum):
    ARCHITECT = "architect"
    BUILDER = "builder"
    REVIEWER = "reviewer"
    TESTER = "tester"
    GENERAL = "general"


class AgentRouteStatus(StrEnum):
    ROUTED = "routed"
    HANDOFF = "handoff"


class AgentRouteReason(StrEnum):
    SPECIALIST_MATCH = "specialist_match"
    GENERAL_FALLBACK = "general_fallback"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class AgentRouteResult:
    prompt: str
    status: AgentRouteStatus
    agent: AgentRole | None
    reason: AgentRouteReason
    candidates: tuple[AgentRole, ...]


_SIGNALS: tuple[tuple[AgentRole, frozenset[str]], ...] = (
    (
        AgentRole.ARCHITECT,
        frozenset(
            {
                "architect",
                "architecture",
                "design",
                "plan",
                "planning",
                "requirements",
                "spec",
                "specification",
            }
        ),
    ),
    (
        AgentRole.BUILDER,
        frozenset(
            {
                "build",
                "builder",
                "code",
                "coding",
                "develop",
                "fix",
                "implement",
                "implementation",
                "refactor",
            }
        ),
    ),
    (
        AgentRole.REVIEWER,
        frozenset({"audit", "critique", "inspect", "review", "reviewer"}),
    ),
    (
        AgentRole.TESTER,
        frozenset(
            {
                "coverage",
                "qa",
                "test",
                "tester",
                "testing",
                "tests",
                "validate",
                "validation",
                "verification",
                "verify",
            }
        ),
    ),
)


class DeterministicAgentRouter:
    """Select exactly one role from whole-word signals or return an explicit handoff."""

    def route(self, request: WorkRequest) -> AgentRouteResult:
        tokens = frozenset(re.findall(r"\w+", request.prompt.casefold()))
        candidates = tuple(role for role, signals in _SIGNALS if tokens & signals)
        if len(candidates) > 1:
            return AgentRouteResult(
                prompt=request.prompt,
                status=AgentRouteStatus.HANDOFF,
                agent=None,
                reason=AgentRouteReason.AMBIGUOUS,
                candidates=candidates,
            )
        if candidates:
            return AgentRouteResult(
                prompt=request.prompt,
                status=AgentRouteStatus.ROUTED,
                agent=candidates[0],
                reason=AgentRouteReason.SPECIALIST_MATCH,
                candidates=candidates,
            )
        return AgentRouteResult(
            prompt=request.prompt,
            status=AgentRouteStatus.ROUTED,
            agent=AgentRole.GENERAL,
            reason=AgentRouteReason.GENERAL_FALLBACK,
            candidates=(AgentRole.GENERAL,),
        )
