from __future__ import annotations

import json
from pathlib import Path

import pytest

from af.application.composition import CompositionGap, CompositionPlanner
from af.framework.context import ExecutionContext
from af.framework.workflows import NodeState, WorkflowError
from af.infrastructure.database import Database
from af.infrastructure.workflow_factory import create_workflow_service, reference_composition


def test_installed_typed_composition_runs_and_reuses_across_restart(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'composition.db'}")
    service = create_workflow_service(database, authentication_key=b"a" * 32)
    plan = reference_composition()
    context = ExecutionContext()
    first = service.submit(
        plan.definition, b'{"texts":["strawberry","raspberry"],"character":"r"}', context
    )
    result = service.resume(first.run_id, context)
    assert all(node.state == NodeState.COMPLETED for node in result.nodes)
    assert json.loads(result.nodes[-1].output) == {"total": 6}
    restarted = create_workflow_service(database, authentication_key=b"a" * 32)
    assert restarted.resume(first.run_id, context).revision == result.revision
    variation = service.submit(plan.definition, b'{"texts":["banana"],"character":"a"}', context)
    assert json.loads(service.resume(variation.run_id, context).nodes[-1].output) == {"total": 3}
    database.close()


def test_planner_reports_missing_type_without_fabricating_component():
    planner = CompositionPlanner(())
    gap = planner.compose("unavailable", "input.v1", "output.v1")
    assert isinstance(gap, CompositionGap)
    assert gap.required_output_schema == "output.v1"
    assert gap.reason == "no_approved_typed_path"


def test_composition_fails_before_execution_for_unavailable_component(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'composition.db'}")
    service = create_workflow_service(database, authentication_key=b"a" * 32)
    plan = reference_composition()
    changed = plan.definition.nodes[0].model_copy(update={"component_digest": "b" * 64})
    definition = plan.definition.model_copy(update={"nodes": (changed, *plan.definition.nodes[1:])})
    with pytest.raises(WorkflowError, match="incompatible"):
        service.submit(definition, b"{}", ExecutionContext())
    database.close()
