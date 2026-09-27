"""Public workflow operations over installed immutable definitions."""

from __future__ import annotations

from af.application.workflows import WorkflowService
from af.framework.context import ExecutionContext
from af.framework.workflows import WorkflowDefinition, WorkflowError, WorkflowRun


class WorkflowController:
    def __init__(
        self, service: WorkflowService, definitions: tuple[WorkflowDefinition, ...]
    ) -> None:
        self.service = service
        self.definitions = {item.workflow_id: item for item in definitions}

    def start(
        self, name: str, data: bytes, context: ExecutionContext, *, max_nodes: int = 128
    ) -> WorkflowRun:
        if type(max_nodes) is not int or not 1 <= max_nodes <= 128:
            raise WorkflowError("max_nodes must be between 1 and 128")
        if type(name) is not str or name not in self.definitions:
            raise WorkflowError("workflow is not installed")
        if type(data) is not bytes or len(data) > 65_536:
            raise WorkflowError("workflow input is too large")
        run = self.service.submit(self.definitions[name], data, context)
        return self.service.resume(run.run_id, context, max_nodes=max_nodes)

    def resume(
        self, run_id: str, context: ExecutionContext, *, max_nodes: int = 128
    ) -> WorkflowRun:
        return self.service.resume(run_id, context, max_nodes=max_nodes)

    def inspect(self, run_id: str, context: ExecutionContext) -> WorkflowRun:
        return self.service.inspect(run_id, context)

    def cancel(self, run_id: str, context: ExecutionContext) -> WorkflowRun:
        return self.service.cancel(run_id, context)
