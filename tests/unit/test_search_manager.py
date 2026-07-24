from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from af.framework.managers import (
    DriverRegistration,
    DriverResolutionError,
    ManagerConfigurationError,
)
from af.search.contracts import ResourceEnvelope, ResourceKind, SearchFilter, SearchQuery
from af.search.embedded_manager import embedded_search_registration
from af.search.manager import SearchFeature, SearchManager

_NOW = datetime(2026, 7, 24, 13, tzinfo=UTC)


def _resource(resource_id: str, *, tenant_id: str = "tenant-a") -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=ResourceKind.TEXT,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{resource_id}",
        tenant_id=tenant_id,
        projection_version=1,
        acl=("group:research",),
        provenance=("sha256:" + "a" * 64,),
        text="bounded searchable energy content",
    )


def _query(*, tenant_id: str = "tenant-a") -> SearchQuery:
    return SearchQuery(
        text="energy",
        filters=(
            SearchFilter("tenant_id", (tenant_id,)),
            SearchFilter("acl", ("group:research",)),
        ),
    )


def _manager(database: Path) -> SearchManager:
    manager = SearchManager(
        connections={
            "primary": {
                "driver": "embedded",
                "database": str(database),
            }
        },
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(embedded_search_registration(clock=lambda: _NOW))
    return manager


def test_search_manager_routes_index_query_and_tombstone_operations(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "search.sqlite3")
    resource = _resource("resource-a")

    assert manager.upsert((resource,)) == 1
    assert manager.search(_query())[0].resource_id == "resource-a"
    assert manager.delete(
        tenant_id="tenant-a",
        source_id="source-primary",
        resource_id="resource-a",
    )
    assert manager.search(_query()) == ()
    assert manager.connection(requires=(SearchFeature.POLICY_PUSHDOWN,)) is manager.connection()

    manager.close()

    with pytest.raises(DriverResolutionError):
        manager.search(_query())


def test_search_manager_isolates_named_connections(tmp_path: Path) -> None:
    manager = SearchManager(
        connections={
            "alpha": {"driver": "embedded", "database": str(tmp_path / "alpha.sqlite3")},
            "beta": {"driver": "embedded", "database": str(tmp_path / "beta.sqlite3")},
        },
        default="alpha",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(embedded_search_registration(clock=lambda: _NOW))

    manager.upsert((_resource("resource-a"),), name="alpha")
    manager.upsert((_resource("resource-b"),), name="beta")

    assert [hit.resource_id for hit in manager.search(_query(), name="alpha")] == ["resource-a"]
    assert [hit.resource_id for hit in manager.search(_query(), name="beta")] == ["resource-b"]

    manager.close()


def test_search_manager_rejects_untargeted_registration(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "search.sqlite3")
    registration = embedded_search_registration(clock=lambda: _NOW)
    untargeted = DriverRegistration(
        manager_id="database",
        driver_name=registration.driver_name,
        factory=registration.factory,
        manifest=registration.manifest,
        verifier=registration.verifier,
        disposer=registration.disposer,
    )

    with pytest.raises(ManagerConfigurationError):
        manager.install(untargeted)

    manager.close()


@pytest.mark.parametrize(
    ("operation", "required"),
    [
        ("search", SearchFeature.FULL_TEXT),
        ("upsert", SearchFeature.INDEXING),
        ("delete", SearchFeature.TOMBSTONES),
    ],
)
def test_search_manager_operations_require_verified_capabilities(
    tmp_path: Path,
    operation: str,
    required: SearchFeature,
) -> None:
    manager = _manager(tmp_path / "search.sqlite3")

    assert manager.connection(requires=(required,)) is manager.connection()
    assert getattr(manager, operation)
    manager.close()
