from __future__ import annotations

from datetime import UTC, datetime

import pytest

from af.infrastructure.database import TraceRow
from af.infrastructure.trace_search import trace_row_to_resource
from af.search.contracts import ResourceKind


def _row() -> TraceRow:
    return TraceRow(
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
        created_at=datetime(2026, 8, 1, 9, tzinfo=UTC),
    )


def test_trace_row_adapter_emits_canonical_relational_resource() -> None:
    row = _row()

    resource = trace_row_to_resource(
        row,
        tenant_id="tenant-a",
        acl=("group:research",),
        projection_version=5,
    )

    assert resource.kind is ResourceKind.RELATIONAL
    assert resource.resource_id == "trace-1"
    assert resource.source_id == "af_run_traces"
    assert resource.authoritative_locator == "af://relational/af_run_traces/trace-1"
    assert resource.tenant_id == "tenant-a"
    assert resource.projection_version == 5
    assert resource.acl == ("group:research",)
    assert resource.text == (
        "how many rs in strawberry\n\n"
        "3\n\n"
        '{"character":"r","text":"strawberry"}\n\n'
        '[{"stage":"classify","detail":"classified"}]'
    )
    assert resource.attributes == {
        "intent": "text.character_count",
        "route": "character-count",
        "tier": "deterministic",
        "status": "completed",
        "verified": "true",
        "reused": "false",
        "regression_passed": "true",
        "promoted": "false",
        "created_at": "2026-08-01T09:00:00Z",
        "table_name": "af_run_traces",
    }
    assert (
        resource.provenance
        == trace_row_to_resource(
            row,
            tenant_id="tenant-a",
            acl=("group:research",),
            projection_version=5,
        ).provenance
    )


def test_trace_row_adapter_normalizes_naive_datetimes_from_relational_backends() -> None:
    row = _row()
    row.created_at = datetime(2026, 8, 1, 9)

    resource = trace_row_to_resource(
        row,
        tenant_id="tenant-a",
        acl=("group:research",),
        projection_version=1,
    )

    assert resource.attributes["created_at"] == "2026-08-01T09:00:00Z"


def test_trace_row_adapter_normalizes_unsafe_document_controls() -> None:
    row = _row()
    row.prompt = "alpha\x00beta"
    row.output = "gamma\u2028delta"
    row.arguments_json = '{"text":"alpha\u2028beta"}'
    row.events_json = '[{"stage":"done","detail":"omega\x00"}]'

    resource = trace_row_to_resource(
        row,
        tenant_id="tenant-a",
        acl=("group:research",),
        projection_version=1,
    )

    assert resource.text == (
        'alpha beta\n\ngamma delta\n\n{"text":"alpha beta"}\n\n[{"stage":"done","detail":"omega "}]'
    )


def test_trace_row_adapter_rejects_non_trace_rows() -> None:
    with pytest.raises(TypeError):
        trace_row_to_resource(  # type: ignore[arg-type]
            object(),
            tenant_id="tenant-a",
            acl=("group:research",),
            projection_version=1,
        )
