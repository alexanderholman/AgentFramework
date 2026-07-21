"""Application boundary for deterministic agent-role routing."""

from __future__ import annotations

from af.domain.agent_routing import AgentRouteResult, DeterministicAgentRouter
from af.framework.types import WorkRequest, normalize_prompt


class AgentRoutingService:
    def __init__(self, router: DeterministicAgentRouter) -> None:
        self._router = router

    def route(self, prompt: object) -> AgentRouteResult:
        normalized = normalize_prompt(prompt)
        return self._router.route(WorkRequest(prompt=normalized))


class AgentRoutingController:
    def __init__(self, service: AgentRoutingService) -> None:
        self._service = service

    def route(self, prompt: object) -> AgentRouteResult:
        return self._service.route(prompt)
