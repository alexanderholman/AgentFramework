from __future__ import annotations

from datetime import UTC, datetime

import pytest

from af.infrastructure.database import TaskRow
from af.infrastructure.task_search import task_row_to_resource
from af.search.contracts import ResourceKind


def _row() -> TaskRow:
    return TaskRow(
        id="task-1",
        prompt="how many rs in strawberry",
        output="3",
        intent="text.character_count",
        route="character-count",
        tier="deterministic",
        status="completed",
        verified=True,
        reused=False,
        trace_id="trace-1",
        created_at=datetime(2026, 7, 31, 12, tzinfo=UTC),
        updated_at=datetime(2026, 7, 31, 13, tzinfo=UTC),
    )


def test_task_row_adapter_emits_canonical_relational_resource() -> None:
    row = _row()

    resource = task_row_to_resource(
        row,
        tenant_id="tenant-a",
        acl=("group:research",),
        projection_version=4,
    )

    assert resource.kind is ResourceKind.RELATIONAL
    assert resource.resource_id == "task-1"
    assert resource.source_id == "af_tasks"
    assert resource.authoritative_locator == "af://relational/af_tasks/task-1"
    assert resource.tenant_id == "tenant-a"
    assert resource.projection_version == 4
    assert resource.acl == ("group:research",)
    assert resource.text == "how many rs in strawberry\n\n3"
    assert resource.attributes == {
        "intent": "text.character_count",
        "route": "character-count",
        "tier": "deterministic",
        "status": "completed",
        "verified": "true",
        "reused": "false",
        "trace_id": "trace-1",
        "created_at": "2026-07-31T12:00:00Z",
        "updated_at": "2026-07-31T13:00:00Z",
        "table_name": "af_tasks",
    }
    assert (
        resource.provenance
        == task_row_to_resource(
            row,
            tenant_id="tenant-a",
            acl=("group:research",),
            projection_version=4,
        ).provenance
    )


def test_task_row_adapter_normalizes_naive_datetimes_from_relational_backends() -> None:
    row = _row()
    row.created_at = datetime(2026, 7, 31, 12)
    row.updated_at = datetime(2026, 7, 31, 13)

    resource = task_row_to_resource(
        row,
        tenant_id="tenant-a",
        acl=("group:research",),
        projection_version=1,
    )

    assert resource.attributes["created_at"] == "2026-07-31T12:00:00Z"
    assert resource.attributes["updated_at"] == "2026-07-31T13:00:00Z"


def test_task_row_adapter_normalizes_unsafe_document_controls() -> None:
    row = _row()
    row.prompt = "alpha\x00beta"
    row.output = "gamma\u2028delta"

    resource = task_row_to_resource(
        row,
        tenant_id="tenant-a",
        acl=("group:research",),
        projection_version=1,
    )

    assert resource.text == "alpha beta\n\ngamma delta"


def test_task_row_adapter_rejects_non_task_rows() -> None:
    with pytest.raises(TypeError):
        task_row_to_resource(  # type: ignore[arg-type]
            object(),
            tenant_id="tenant-a",
            acl=("group:research",),
            projection_version=1,
        )
