from __future__ import annotations

from datetime import UTC, datetime

from af.infrastructure.database import Database, TraceRow
from af.infrastructure.trace_search import trace_row_to_resource
from af.search.contracts import SearchFilter, SearchQuery
from af.search.embedded import EmbeddedSearchDriver


def test_trace_relational_row_enters_canonical_search_api() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    created_at = datetime(2026, 8, 1, 9, tzinfo=UTC)
    with database.sessions.begin() as session:
        session.add(
            TraceRow(
                id="trace-1",
                prompt="how many rs in strawberry",
                intent="text.character_count",
                arguments_json='{"character":"r","text":"strawberry"}',
                route="character-count",
                tier="deterministic",
                output="3",
                verified=True,
                reused=False,
                regression_passed=True,
                promoted=False,
                status="completed",
                events_json='[{"stage":"classify","detail":"classified"}]',
                created_at=created_at,
            )
        )
    with database.sessions() as session:
        row = session.get(TraceRow, "trace-1")
        assert row is not None
        resource = trace_row_to_resource(
            row,
            tenant_id="tenant-a",
            acl=("group:research",),
            projection_version=2,
        )

    with EmbeddedSearchDriver(":memory:") as driver:
        assert driver.upsert((resource,)) == 1
        hits = driver.search(
            SearchQuery(
                text="classified",
                filters=(
                    SearchFilter("tenant_id", ("tenant-a",)),
                    SearchFilter("acl", ("group:research",)),
                ),
            )
        )

    assert len(hits) == 1
    assert hits[0].resource_id == "trace-1"
    assert hits[0].source_id == "af_run_traces"
    assert hits[0].authoritative_locator == "af://relational/af_run_traces/trace-1"
    assert hits[0].projection_version == 2
    assert hits[0].provenance == resource.provenance


def test_trace_relational_row_with_unsafe_controls_still_indexes() -> None:
    database = Database("sqlite+pysqlite:///:memory:")
    with database.sessions.begin() as session:
        session.add(
            TraceRow(
                id="trace-unsafe",
                prompt="alpha\x00beta",
                intent="text.character_count",
                arguments_json='{"text":"beta\u2028gamma"}',
                route="character-count",
                tier="deterministic",
                output="gamma\u2028delta",
                verified=True,
                reused=False,
                regression_passed=True,
                promoted=False,
                status="completed",
                events_json='[{"stage":"done","detail":"omega\x00"}]',
                created_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
            )
        )
    with database.sessions() as session:
        row = session.get(TraceRow, "trace-unsafe")
        assert row is not None
        resource = trace_row_to_resource(
            row,
            tenant_id="tenant-a",
            acl=("group:research",),
            projection_version=2,
        )

    with EmbeddedSearchDriver(":memory:") as driver:
        assert driver.upsert((resource,)) == 1
        hits = driver.search(
            SearchQuery(
                text="omega",
                filters=(
                    SearchFilter("tenant_id", ("tenant-a",)),
                    SearchFilter("acl", ("group:research",)),
                ),
            )
        )

    assert len(hits) == 1
    assert hits[0].resource_id == "trace-unsafe"
