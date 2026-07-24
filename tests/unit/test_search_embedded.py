from __future__ import annotations

from pathlib import Path

import pytest

from af.framework.errors import PlatformReason
from af.search.contracts import ResourceEnvelope, ResourceKind, SearchFilter, SearchQuery
from af.search.embedded import EmbeddedSearchDriver, SearchDriverIneligible


def _resource(
    resource_id: str,
    *,
    kind: ResourceKind = ResourceKind.TEXT,
    tenant_id: str = "tenant-a",
    acl: tuple[str, ...] = ("group:research",),
    text: str = "bounded searchable energy content",
    projection_version: int = 1,
) -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=kind,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{resource_id}",
        tenant_id=tenant_id,
        projection_version=projection_version,
        acl=acl,
        provenance=("sha256:" + resource_id[-1] * 64,),
        text=text,
        attributes={"language": "en"},
    )


def _query(
    text: str = "energy",
    *,
    tenant_id: str = "tenant-a",
    acl: tuple[str, ...] = ("group:research",),
    limit: int = 20,
) -> SearchQuery:
    return SearchQuery(
        text=text,
        filters=(
            SearchFilter("tenant_id", (tenant_id,)),
            SearchFilter("acl", acl),
        ),
        limit=limit,
        deadline_ms=1_000,
    )


def test_embedded_search_indexes_every_canonical_source_and_persists(tmp_path: Path) -> None:
    database = tmp_path / "search.sqlite3"
    resources = tuple(
        _resource(f"resource-{index}", kind=kind)
        for index, kind in enumerate(ResourceKind, start=1)
    )

    with EmbeddedSearchDriver(database) as driver:
        assert driver.upsert(resources) == len(resources)
        hits = driver.search(_query())

    with EmbeddedSearchDriver(database) as reopened:
        persisted = reopened.search(_query())

    assert [hit.resource_id for hit in hits] == [hit.resource_id for hit in persisted]
    assert {hit.resource_id for hit in hits} == {item.resource_id for item in resources}
    assert all(hit.acl_evidence == ("group:research",) for hit in hits)
    assert all(hit.provenance for hit in hits)


def test_policy_filters_are_enforced_before_top_k(tmp_path: Path) -> None:
    allowed = _resource(
        "resource-a",
        text="energy",
        acl=("group:allowed",),
    )
    denied = tuple(
        _resource(
            f"resource-{suffix}",
            text="energy energy energy energy",
            acl=("group:denied",),
        )
        for suffix in ("b", "c", "d")
    )

    with EmbeddedSearchDriver(tmp_path / "policy.sqlite3") as driver:
        driver.upsert((allowed, *denied))
        hits = driver.search(_query(acl=("group:allowed",), limit=1))

    assert tuple(hit.resource_id for hit in hits) == ("resource-a",)


def test_upsert_replaces_projection_and_delete_tombstones_identity(tmp_path: Path) -> None:
    first = _resource("resource-a", text="energy", projection_version=1)
    replacement = _resource("resource-a", text="updated energy", projection_version=2)

    with EmbeddedSearchDriver(tmp_path / "lifecycle.sqlite3") as driver:
        driver.upsert((first,))
        driver.upsert((replacement,))

        hits = driver.search(_query("updated"))
        removed = driver.delete(
            tenant_id="tenant-a",
            source_id="source-primary",
            resource_id="resource-a",
        )
        after_delete = driver.search(_query("updated"))

    assert len(hits) == 1
    assert hits[0].projection_version == 2
    assert removed is True
    assert after_delete == ()


@pytest.mark.parametrize(
    "filters",
    [
        (SearchFilter("tenant_id", ("tenant-a",)),),
        (SearchFilter("acl", ("group:research",)),),
        (
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
            SearchFilter("language", ("en",)),
        ),
    ],
)
def test_embedded_search_rejects_queries_it_cannot_filter_before_ranking(
    tmp_path: Path,
    filters: tuple[SearchFilter, ...],
) -> None:
    query = SearchQuery(text="energy", filters=filters)

    with EmbeddedSearchDriver(tmp_path / "ineligible.sqlite3") as driver:
        with pytest.raises(SearchDriverIneligible) as captured:
            driver.search(query)

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert str(captured.value) == "required capability is unavailable"


def test_embedded_search_escapes_fts_syntax_and_sanitizes_closed_driver(
    tmp_path: Path,
) -> None:
    driver = EmbeddedSearchDriver(tmp_path / "syntax.sqlite3")
    driver.upsert((_resource("resource-a", text='literal energy "value"'),))

    assert driver.search(_query('"energy" OR *')) == ()
    driver.close()

    with pytest.raises(SearchDriverIneligible) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED
    assert "sqlite" not in str(captured.value).lower()
