"""Shared structured transport output for workflow summaries."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, JsonValue

from af.framework.workflows import WorkflowRun
from af.interfaces.workflows import workflow_to_dict


class WorkflowNodeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    state: str
    verified: bool


class WorkflowOutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node: str
    value: JsonValue


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: str
    run_id: str
    revision: int
    complete: bool
    cancelled: bool
    nodes: list[WorkflowNodeModel]
    outputs: list[WorkflowOutputModel]

    @classmethod
    def from_run(cls, run: WorkflowRun) -> WorkflowModel:
        return cls.model_validate(workflow_to_dict(run))
