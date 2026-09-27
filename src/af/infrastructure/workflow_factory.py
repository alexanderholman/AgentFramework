"""Explicit composition helper for the installed reference workflow profile."""

from __future__ import annotations

from af.application.composition import CompiledComposition, ComponentContract, CompositionPlanner
from af.application.workflows import WorkflowService
from af.domain.workflow_components import reference_workflow_components
from af.framework.workflows import WorkflowPolicy, WorkflowResources
from af.infrastructure.database import Database
from af.infrastructure.workflows import SqlAlchemyWorkflowRepository


def create_workflow_service(
    database: Database,
    *,
    authentication_key: bytes,
    policy: WorkflowPolicy | None = None,
    resources: WorkflowResources | None = None,
) -> WorkflowService:
    return WorkflowService(
        SqlAlchemyWorkflowRepository(database, authentication_key=authentication_key),
        reference_workflow_components(),
        policy=policy,
        resources=resources,
    )


def reference_composition() -> CompiledComposition:
    batch, total = reference_workflow_components()
    planner = CompositionPlanner(
        (
            ComponentContract(batch, "text.count-batch.v1", "number.vector.v1"),
            ComponentContract(total, "number.vector.v1", "number.scalar.v1"),
        )
    )
    result = planner.compose("text.count-total", "text.count-batch.v1", "number.scalar.v1")
    if not isinstance(result, CompiledComposition):
        raise RuntimeError("installed reference composition is incomplete")
    return result
