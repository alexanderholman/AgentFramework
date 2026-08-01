from __future__ import annotations

from datetime import UTC, datetime

from af.infrastructure.database import Database, TaskRow
from af.infrastructure.task_search import task_row_to_resource
from af.search.contracts import SearchFilter, SearchQuery
from af.search.embedded import EmbeddedSearchDriver


def test_task_relational_row_enters_canonical_search_api() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    created_at = datetime(2026, 7, 31, 12, tzinfo=UTC)
    updated_at = datetime(2026, 7, 31, 13, tzinfo=UTC)
    with database.sessions.begin() as session:
        session.add(
            TaskRow(
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
                created_at=created_at,
                updated_at=updated_at,
            )
        )
    with database.sessions() as session:
        row = session.get(TaskRow, "task-1")
        assert row is not None
        resource = task_row_to_resource(
            row,
            tenant_id="tenant-a",
            acl=("group:research",),
            projection_version=2,
        )

    with EmbeddedSearchDriver(":memory:") as driver:
        assert driver.upsert((resource,)) == 1
        hits = driver.search(
            SearchQuery(
                text="strawberry",
                filters=(
                    SearchFilter("tenant_id", ("tenant-a",)),
                    SearchFilter("acl", ("group:research",)),
                ),
            )
        )

    assert len(hits) == 1
    assert hits[0].resource_id == "task-1"
    assert hits[0].source_id == "af_tasks"
    assert hits[0].authoritative_locator == "af://relational/af_tasks/task-1"
    assert hits[0].projection_version == 2
    assert hits[0].provenance == resource.provenance
