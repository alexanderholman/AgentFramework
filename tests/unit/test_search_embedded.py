from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

import af.search.embedded as embedded_module
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
    assert all(hit.tenant_id == "tenant-a" for hit in hits)
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


def test_tenant_is_part_of_hit_identity_for_colliding_resources(tmp_path: Path) -> None:
    tenant_a = _resource("resource-a", tenant_id="tenant-a")
    tenant_b = _resource("resource-a", tenant_id="tenant-b")

    with EmbeddedSearchDriver(tmp_path / "tenant-identity.sqlite3") as driver:
        driver.upsert((tenant_a, tenant_b))
        hits = driver.search(_query(tenant_id="tenant-b"))

    assert len(hits) == 1
    assert hits[0].resource_id == "resource-a"
    assert hits[0].tenant_id == "tenant-b"


def test_cross_tenant_tie_order_does_not_depend_on_insertion_order(tmp_path: Path) -> None:
    tenant_a = _resource("resource-a", tenant_id="tenant-a")
    tenant_b = _resource("resource-a", tenant_id="tenant-b")
    query = SearchQuery(
        text="energy",
        filters=(
            SearchFilter("tenant_id", ("tenant-a", "tenant-b")),
            SearchFilter("acl", ("group:research",)),
        ),
    )
    orders: list[tuple[str, ...]] = []

    for database, resources in (
        (tmp_path / "forward.sqlite3", (tenant_a, tenant_b)),
        (tmp_path / "reverse.sqlite3", (tenant_b, tenant_a)),
    ):
        with EmbeddedSearchDriver(database) as driver:
            driver.upsert(resources)
            orders.append(tuple(hit.tenant_id for hit in driver.search(query)))

    assert orders == [
        ("tenant-a", "tenant-b"),
        ("tenant-a", "tenant-b"),
    ]


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
        (
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
            SearchFilter("language", ("en",)),
        ),
        (
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
            SearchFilter("format", ("markdown",)),
        ),
    ],
)
def test_embedded_search_rejects_queries_it_cannot_filter_before_ranking(
    tmp_path: Path,
    filters: tuple[SearchFilter, ...],
) -> None:
    query = SearchQuery(text="energy", filters=filters)

    with (
        EmbeddedSearchDriver(tmp_path / "ineligible.sqlite3") as driver,
        pytest.raises(SearchDriverIneligible) as captured,
    ):
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


def test_embedded_search_rejects_invalid_operations_and_is_idempotent() -> None:
    with pytest.raises(ValueError):
        EmbeddedSearchDriver("")

    resource = _resource("resource-a")
    driver = EmbeddedSearchDriver(":memory:")

    with pytest.raises(ValueError):
        driver.upsert(())
    with pytest.raises(ValueError):
        driver.upsert((resource, resource))
    with pytest.raises(TypeError):
        driver.search(object())  # type: ignore[arg-type]
    with pytest.raises(SearchDriverIneligible) as captured:
        driver.search(_query("!!!"))

    assert captured.value.reason is PlatformReason.VALIDATION_FAILED
    assert (
        driver.delete(
            tenant_id="tenant-a",
            source_id="source-primary",
            resource_id="missing",
        )
        is False
    )
    driver.cancel()
    driver.close()
    driver.close()


def test_manual_interrupt_is_reported_as_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    calls = 0

    def controlled_monotonic() -> float:
        nonlocal calls
        calls += 1
        if calls > 1:
            started.set()
            assert release.wait(timeout=5)
        return 0.0

    with EmbeddedSearchDriver(tmp_path / "cancel.sqlite3") as driver:
        driver.upsert(tuple(_resource(f"resource-{index}") for index in range(200)))
        monkeypatch.setattr(embedded_module, "monotonic", controlled_monotonic)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(driver.search, _query())
            assert started.wait(timeout=5)
            driver.cancel()
            release.set()

            with pytest.raises(SearchDriverIneligible) as captured:
                pending.result(timeout=5)

        assert driver.search(_query())

    assert captured.value.reason is PlatformReason.CANCELLED
    assert str(captured.value) == "operation was cancelled"


def test_non_interrupt_error_after_deadline_remains_permanent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def elapsed_monotonic() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 2.0

    with EmbeddedSearchDriver(tmp_path / "missing-index.sqlite3") as driver:
        driver._connection.execute("DROP TABLE af_search_fts")
        monkeypatch.setattr(embedded_module, "monotonic", elapsed_monotonic)

        with pytest.raises(SearchDriverIneligible) as captured:
            driver.search(_query())

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert str(captured.value) == "operation failed"
    assert "table" not in str(captured.value)


def test_interrupted_upsert_rolls_back_and_recovers(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    def pause_write() -> int:
        started.set()
        assert release.wait(timeout=5)
        return 0

    with EmbeddedSearchDriver(tmp_path / "upsert-cancel.sqlite3") as driver:
        driver._connection.set_progress_handler(pause_write, 1_000)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                driver.upsert,
                tuple(_resource(f"resource-{index}") for index in range(200)),
            )
            assert started.wait(timeout=5)
            try:
                driver.cancel()
            finally:
                release.set()

            with pytest.raises(SearchDriverIneligible) as captured:
                pending.result(timeout=5)

        driver._connection.set_progress_handler(None, 0)
        driver.upsert((_resource("resource-f"),))
        recovered = driver.search(_query())

    assert captured.value.reason is PlatformReason.CANCELLED
    assert tuple(hit.resource_id for hit in recovered) == ("resource-f",)


def test_interrupted_delete_preserves_resource_and_reports_cancellation(
    tmp_path: Path,
) -> None:
    started = Event()
    release = Event()

    def pause_delete() -> int:
        started.set()
        assert release.wait(timeout=5)
        return 0

    with EmbeddedSearchDriver(tmp_path / "delete-cancel.sqlite3") as driver:
        driver.upsert((_resource("resource-a"),))
        driver._connection.set_progress_handler(pause_delete, 1)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                driver.delete,
                tenant_id="tenant-a",
                source_id="source-primary",
                resource_id="resource-a",
            )
            assert started.wait(timeout=5)
            try:
                driver.cancel()
            finally:
                release.set()

            with pytest.raises(SearchDriverIneligible) as captured:
                pending.result(timeout=5)

        driver._connection.set_progress_handler(None, 0)
        recovered = driver.search(_query())

    assert captured.value.reason is PlatformReason.CANCELLED
    assert tuple(hit.resource_id for hit in recovered) == ("resource-a",)


def test_cancel_is_idempotent_once_concurrent_shutdown_begins() -> None:
    connection_closed = Event()
    release_close = Event()

    class CoordinatedConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            connection_closed.set()
            assert release_close.wait(timeout=5)

    driver = EmbeddedSearchDriver(":memory:")
    driver._connection.close()
    driver._connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=CoordinatedConnection,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        closing = pool.submit(driver.close)
        assert connection_closed.wait(timeout=5)
        try:
            driver.cancel()
        finally:
            release_close.set()
        closing.result(timeout=5)

    driver.cancel()
    driver.close()


def test_active_cancel_failure_is_sanitized() -> None:
    class RejectingInterruptConnection(sqlite3.Connection):
        def interrupt(self) -> None:
            raise sqlite3.OperationalError("sensitive sqlite cancellation failure")

    driver = EmbeddedSearchDriver(":memory:")
    driver._connection.close()
    driver._connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=RejectingInterruptConnection,
    )

    with pytest.raises(SearchDriverIneligible) as captured:
        driver.cancel()

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.embedded.cancel"
    assert str(captured.value) == "operation failed"
    assert "sqlite" not in str(captured.value).lower()
    driver.close()
