"""Configured shared boundary for explicit finite local engineering."""

from __future__ import annotations

from af.application.engineering import LocalEngineeringService
from af.framework.context import ExecutionContext
from af.framework.engineering import (
    FAMILY,
    ComponentAdmission,
    EngineeringCompletion,
    EngineeringRequest,
)
from af.framework.workflows import WorkflowError


class EngineeringController:
    def __init__(self, service: LocalEngineeringService | None) -> None:
        self._service = service

    def _configured(self) -> LocalEngineeringService:
        if self._service is None:
            raise WorkflowError("local_engineering_repository_not_configured")
        return self._service

    def complete(
        self, request: EngineeringRequest, context: ExecutionContext
    ) -> EngineeringCompletion:
        return self._configured().complete(request, context)

    def inspect(self) -> ComponentAdmission | None:
        return self._configured().admitted(FAMILY)

    def invalidate(self, reason: str, context: ExecutionContext) -> None:
        self._configured().invalidate(FAMILY, reason, context)
