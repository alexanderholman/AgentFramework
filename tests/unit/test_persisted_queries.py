from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from af.framework.persisted_queries import PersistedQuery, validate_digest
from af.infrastructure.sqlite_queries import SQLitePersistedQueryRegistry
from af.interfaces.graphql import approve_graphql_query


def test_capacity_idempotency_conflicting_approval_and_corruption(tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "registry.sqlite"
    with SQLitePersistedQueryRegistry(path, max_queries=1) as registry:
        approved = approve_graphql_query(registry, "{ tasks { id } }")
        registry.approve(approved)
        with pytest.raises(ValueError, match="revoke"):
            registry.approve(replace(approved, schema_digest="1" * 64))
        with pytest.raises(ValueError, match="full"):
            approve_graphql_query(registry, "{ tasks { prompt } }")
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE af_persisted_queries SET document=?", ("{ tasks { prompt } }",))
        with pytest.raises(ValueError, match="integrity"):
            registry.resolve(approved.digest)


@pytest.mark.parametrize("capacity", [0, -1, True, 100_001])
def test_invalid_registry_capacity(capacity):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        SQLitePersistedQueryRegistry(":memory:", max_queries=capacity)


@pytest.mark.parametrize("value", ["", "A" * 64, "0" * 63, None, 1])
def test_invalid_query_digest(value):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        validate_digest(value)


@pytest.mark.parametrize("document", ["", "x" * 65_537, None])
def test_invalid_document_bounds(document):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        PersistedQuery(document, "0" * 64)


@pytest.mark.parametrize("scope", ["", "x" * 201, "bad\nvalue", 42])
def test_invalid_query_scope(scope):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        PersistedQuery("{ tasks { id } }", "0" * 64, tenant_id=scope)


def test_approval_rejects_invalid_schema_before_persisting() -> None:
    with (
        SQLitePersistedQueryRegistry(":memory:") as registry,
        pytest.raises(ValueError, match="schema"),
    ):
        approve_graphql_query(registry, "{ nonexistent }")
