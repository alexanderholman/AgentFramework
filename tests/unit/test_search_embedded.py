from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import af.search.embedded as embedded_module
import pytest
from af.framework.errors import PlatformReason
from af.search.contracts import (
    ResourceEnvelope,
    ResourceKind,
    SearchFilter,
    SearchQuery,
)
from af.search.embedded import EmbeddedSearchDriver, SearchDriverIneligible


def _resource(
    resource_id: str,
    *,
    kind: ResourceKind = ResourceKind.TEXT,
    tenant_id: str = "tenant-a",
    acl: tuple[str, ...] = ("group:research",),
    text: str = "bounded searchable energy content",
    projection_version: int = 1,
    attributes: dict[str, str] | None = None,
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
        attributes={"language": "en"} if attributes is None else attributes,
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


def test_representative_source_fixtures_enter_the_canonical_search_api(tmp_path: Path) -> None:
    fixtures = (
        (
            ResourceKind.MARKDOWN,
            "# Interface notes\\nEnergy interface fixture",
            {"format": "markdown", "heading": "interface-notes"},
        ),
        (
            ResourceKind.RELATIONAL,
            "row id=record-1 value=energy",
            {"table_name": "materials", "row_id": "record-1"},
        ),
        (
            ResourceKind.BSON,
            '{"document_type":"bson","summary":"energy"}',
            {"format": "bson", "document_type": "material"},
        ),
        (
            ResourceKind.GRAPH,
            "node material connected by interface edge energy",
            {"node_label": "material", "edge_label": "interface"},
        ),
        (
            ResourceKind.CACHE,
            "cache key=material:1 value=energy",
            {"cache_key": "material-1", "encoding": "text"},
        ),
        (
            ResourceKind.BLOB,
            "media extract OCR transcription energy",
            {"media_type": "image/png", "extractor": "ocr"},
        ),
        (
            ResourceKind.EVENT,
            "event type=material.updated energy",
            {"event_type": "material.updated", "sequence": "42"},
        ),
        (
            ResourceKind.TIME_SERIES,
            "2026-09-24T12:00:00Z temperature 300K energy",
            {"series": "temperature", "timestamp": "2026-09-24T12:00:00Z"},
        ),
        (
            ResourceKind.TEXT,
            "plain text document energy",
            {"language": "en", "format": "text"},
        ),
        (
            ResourceKind.VECTOR,
            "embedding 0.1 0.2 0.3 energy",
            {"dimensions": "3", "encoding": "float32"},
        ),
    )
    resources = tuple(
        _resource(
            f"fixture-{index}",
            kind=kind,
            text=text,
            attributes=attributes,
        )
        for index, (kind, text, attributes) in enumerate(fixtures, start=1)
    )

    with EmbeddedSearchDriver(tmp_path / "canonical-source-fixtures.sqlite3") as driver:
        assert driver.upsert(resources) == len(fixtures)
        hits = driver.search(_query("energy"))

    assert {hit.resource_id for hit in hits} == {item.resource_id for item in resources}
    assert {item.kind for item in resources} == set(ResourceKind)
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


def test_multiline_document_returns_contract_safe_highlight(tmp_path: Path) -> None:
    resource = _resource(
        "resource-a",
        text="stored\nenergy\tfor\r\nfuture demand",
    )

    with EmbeddedSearchDriver(tmp_path / "multiline-highlight.sqlite3") as driver:
        driver.upsert((resource,))
        hits = driver.search(_query())

    assert len(hits) == 1
    assert hits[0].highlights == ("stored <em>energy</em> for future demand",)


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


def test_progress_handler_setup_failure_is_sanitized_and_quarantined(
    tmp_path: Path,
) -> None:
    database = tmp_path / "progress-setup-failure.sqlite3"

    class SetupFailingConnection(sqlite3.Connection):
        def set_progress_handler(
            self,
            progress_handler: Callable[[], int | None] | None,
            n: int,
        ) -> None:
            if progress_handler is not None:
                raise sqlite3.OperationalError("sensitive progress setup failure")
            super().set_progress_handler(progress_handler, n)

    driver = EmbeddedSearchDriver(database)
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=SetupFailingConnection,
    )

    with pytest.raises(SearchDriverIneligible) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.embedded.query.progress-setup"
    assert "progress" not in str(captured.value)
    with pytest.raises(SearchDriverIneligible) as unavailable:
        driver.search(_query())
    assert unavailable.value.reason is PlatformReason.SHUTDOWN_FAILED
    driver.cancel()
    driver.close()


def test_cancelled_query_preserves_precedence_when_progress_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "progress-cleanup-failure.sqlite3"
    started = Event()
    release = Event()
    calls = 0

    class CleanupFailingConnection(sqlite3.Connection):
        def set_progress_handler(
            self,
            progress_handler: Callable[[], int | None] | None,
            n: int,
        ) -> None:
            if progress_handler is None:
                raise sqlite3.OperationalError("sensitive progress cleanup failure")
            super().set_progress_handler(progress_handler, n)

    def controlled_monotonic() -> float:
        nonlocal calls
        calls += 1
        if calls > 1:
            started.set()
            assert release.wait(timeout=5)
        return 0.0

    driver = EmbeddedSearchDriver(database)
    driver.upsert(tuple(_resource(f"resource-{index}") for index in range(200)))
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=CleanupFailingConnection,
    )
    monkeypatch.setattr(embedded_module, "monotonic", controlled_monotonic)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(driver.search, _query())
        assert started.wait(timeout=5)
        try:
            driver.cancel()
        finally:
            release.set()

        with pytest.raises(SearchDriverIneligible) as captured:
            pending.result(timeout=5)

    assert captured.value.reason is PlatformReason.CANCELLED
    assert captured.value.stage == "search.embedded.query"
    with pytest.raises(SearchDriverIneligible) as unavailable:
        driver.search(_query())
    assert unavailable.value.reason is PlatformReason.SHUTDOWN_FAILED
    driver.cancel()
    driver.close()

    with EmbeddedSearchDriver(database) as recovered:
        assert recovered.search(_query())


def test_progress_cleanup_failure_is_sanitized_and_quarantined(tmp_path: Path) -> None:
    database = tmp_path / "progress-cleanup-standalone-failure.sqlite3"

    class CleanupFailingConnection(sqlite3.Connection):
        def set_progress_handler(
            self,
            progress_handler: Callable[[], int | None] | None,
            n: int,
        ) -> None:
            if progress_handler is None:
                raise sqlite3.OperationalError("sensitive progress cleanup failure")
            super().set_progress_handler(progress_handler, n)

    driver = EmbeddedSearchDriver(database)
    driver.upsert((_resource("resource-a"),))
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=CleanupFailingConnection,
    )

    with pytest.raises(SearchDriverIneligible) as captured:
        driver.search(_query())

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.embedded.query.progress-cleanup"
    assert "progress" not in str(captured.value)
    with pytest.raises(SearchDriverIneligible) as unavailable:
        driver.search(_query())
    assert unavailable.value.reason is PlatformReason.SHUTDOWN_FAILED
    driver.close()

    with EmbeddedSearchDriver(database) as recovered:
        assert recovered.search(_query())


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


def test_cancelled_upsert_preserves_precedence_when_rollback_fails(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upsert-cancel-rollback-failure.sqlite3"
    started = Event()
    release = Event()

    class RollbackFailingConnection(sqlite3.Connection):
        def rollback(self) -> None:
            raise sqlite3.OperationalError("sensitive rollback failure")

    def pause_write() -> int:
        started.set()
        assert release.wait(timeout=5)
        return 0

    driver = EmbeddedSearchDriver(database)
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=RollbackFailingConnection,
    )
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

    assert captured.value.reason is PlatformReason.CANCELLED
    assert captured.value.stage == "search.embedded.upsert"
    with pytest.raises(SearchDriverIneligible) as unavailable:
        driver.search(_query())
    assert unavailable.value.reason is PlatformReason.SHUTDOWN_FAILED
    driver.cancel()
    driver.close()

    with EmbeddedSearchDriver(database) as recovered:
        assert recovered.search(_query()) == ()


def test_base_exception_during_upsert_rolls_back_and_recovers(tmp_path: Path) -> None:
    database = tmp_path / "upsert-base-exception.sqlite3"

    class InterruptingConnection(sqlite3.Connection):
        interrupt_next_insert = True

        def execute(  # type: ignore[override]
            self,
            sql: str,
            parameters: tuple[object, ...] = (),
            /,
        ) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if self.interrupt_next_insert and "INSERT INTO af_search_fts" in sql:
                self.interrupt_next_insert = False
                raise KeyboardInterrupt
            return cursor

    driver = EmbeddedSearchDriver(database)
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=InterruptingConnection,
    )

    with pytest.raises(KeyboardInterrupt):
        driver.upsert((_resource("resource-a"),))

    assert driver._connection.in_transaction is False
    assert driver.search(_query()) == ()
    assert driver.upsert((_resource("resource-b"),)) == 1
    assert tuple(hit.resource_id for hit in driver.search(_query())) == ("resource-b",)
    driver.close()


def test_rollback_failure_quarantines_partial_upsert_and_preserves_interrupt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upsert-rollback-failure.sqlite3"

    class RollbackFailingConnection(sqlite3.Connection):
        interrupt_next_insert = True

        def execute(  # type: ignore[override]
            self,
            sql: str,
            parameters: tuple[object, ...] = (),
            /,
        ) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if self.interrupt_next_insert and "INSERT INTO af_search_fts" in sql:
                self.interrupt_next_insert = False
                raise KeyboardInterrupt
            return cursor

        def rollback(self) -> None:
            raise sqlite3.OperationalError("sensitive rollback failure")

    driver = EmbeddedSearchDriver(database)
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=RollbackFailingConnection,
    )

    with pytest.raises(KeyboardInterrupt):
        driver.upsert((_resource("resource-a"),))

    with pytest.raises(SearchDriverIneligible) as unavailable:
        driver.search(_query())

    assert unavailable.value.reason is PlatformReason.SHUTDOWN_FAILED
    assert "rollback" not in str(unavailable.value).lower()
    driver.close()

    with EmbeddedSearchDriver(database) as recovered:
        assert recovered.search(_query()) == ()


def test_ordinary_rollback_failure_is_sanitized_and_quarantined(
    tmp_path: Path,
) -> None:
    database = tmp_path / "upsert-ordinary-rollback-failure.sqlite3"

    class RollbackFailingConnection(sqlite3.Connection):
        fail_next_insert = True

        def execute(  # type: ignore[override]
            self,
            sql: str,
            parameters: tuple[object, ...] = (),
            /,
        ) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if self.fail_next_insert and "INSERT INTO af_search_fts" in sql:
                self.fail_next_insert = False
                raise sqlite3.OperationalError("sensitive write failure")
            return cursor

        def rollback(self) -> None:
            raise sqlite3.OperationalError("sensitive rollback failure")

    driver = EmbeddedSearchDriver(database)
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=RollbackFailingConnection,
    )

    with pytest.raises(SearchDriverIneligible) as captured:
        driver.upsert((_resource("resource-a"),))

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.embedded.upsert.rollback"
    assert "sensitive" not in str(captured.value)
    with pytest.raises(SearchDriverIneligible) as unavailable:
        driver.search(_query())
    assert unavailable.value.reason is PlatformReason.SHUTDOWN_FAILED
    driver.close()

    with EmbeddedSearchDriver(database) as recovered:
        assert recovered.search(_query()) == ()


def test_rollback_interruption_quarantines_partial_upsert(tmp_path: Path) -> None:
    database = tmp_path / "upsert-rollback-interruption.sqlite3"

    class RollbackInterruptedConnection(sqlite3.Connection):
        interrupt_next_insert = True

        def execute(  # type: ignore[override]
            self,
            sql: str,
            parameters: tuple[object, ...] = (),
            /,
        ) -> sqlite3.Cursor:
            cursor = super().execute(sql, parameters)
            if self.interrupt_next_insert and "INSERT INTO af_search_fts" in sql:
                self.interrupt_next_insert = False
                raise sqlite3.OperationalError("sensitive write failure")
            return cursor

        def rollback(self) -> None:
            raise KeyboardInterrupt

    driver = EmbeddedSearchDriver(database)
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=RollbackInterruptedConnection,
    )

    with pytest.raises(KeyboardInterrupt):
        driver.upsert((_resource("resource-a"),))

    with pytest.raises(SearchDriverIneligible) as unavailable:
        driver.search(_query())

    assert unavailable.value.reason is PlatformReason.SHUTDOWN_FAILED
    driver.close()

    with EmbeddedSearchDriver(database) as recovered:
        assert recovered.search(_query()) == ()


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


@pytest.mark.parametrize(
    "control_flow",
    [
        KeyboardInterrupt(),
        SearchDriverIneligible(PlatformReason.CANCELLED, stage="test.cancel"),
    ],
)
def test_cancel_preserves_control_flow_when_shutdown_wins_race(
    control_flow: BaseException,
) -> None:
    interrupt_started = Event()
    connection_closed = Event()

    class ControlFlowInterruptConnection(sqlite3.Connection):
        def interrupt(self) -> None:
            interrupt_started.set()
            assert connection_closed.wait(timeout=5)
            raise control_flow

        def close(self) -> None:
            super().close()
            connection_closed.set()

    driver = EmbeddedSearchDriver(":memory:")
    driver._connection.close()
    driver._connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=ControlFlowInterruptConnection,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelling = pool.submit(driver.cancel)
        assert interrupt_started.wait(timeout=5)
        closing = pool.submit(driver.close)
        closing.result(timeout=5)
        captured: BaseException | None = None

        try:
            cancelling.result(timeout=5)
        except BaseException as error:
            captured = error

    assert captured is control_flow


def test_cancel_suppresses_sqlite_error_when_shutdown_wins_race() -> None:
    interrupt_started = Event()
    connection_closed = Event()

    class ClosedInterruptConnection(sqlite3.Connection):
        def interrupt(self) -> None:
            interrupt_started.set()
            assert connection_closed.wait(timeout=5)
            raise sqlite3.ProgrammingError("sensitive closed connection")

        def close(self) -> None:
            super().close()
            connection_closed.set()

    driver = EmbeddedSearchDriver(":memory:")
    driver._connection.close()
    driver._connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=ClosedInterruptConnection,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelling = pool.submit(driver.cancel)
        assert interrupt_started.wait(timeout=5)
        closing = pool.submit(driver.close)
        closing.result(timeout=5)
        assert cancelling.result(timeout=5) is None


def test_base_exception_during_close_allows_cleanup_retry() -> None:
    class InterruptingCloseConnection(sqlite3.Connection):
        close_attempts = 0

        def close(self) -> None:
            self.close_attempts += 1
            if self.close_attempts == 1:
                raise KeyboardInterrupt
            super().close()

    driver = EmbeddedSearchDriver(":memory:")
    driver._connection.close()
    driver._connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=InterruptingCloseConnection,
    )

    with pytest.raises(KeyboardInterrupt):
        driver.close()

    assert driver._closing is False
    assert driver._closed is False
    driver.close()
    assert driver._closed is True


def test_partial_close_failure_quarantines_all_operations_and_retries(
    tmp_path: Path,
) -> None:
    database = tmp_path / "partial-close.sqlite3"

    class PartialCloseConnection(sqlite3.Connection):
        close_attempts = 0

        def close(self) -> None:
            self.close_attempts += 1
            super().close()
            if self.close_attempts == 1:
                raise sqlite3.OperationalError("sensitive partial close failure")

    driver = EmbeddedSearchDriver(database)
    driver._connection.close()
    driver._connection = sqlite3.connect(
        database,
        isolation_level=None,
        check_same_thread=False,
        factory=PartialCloseConnection,
    )

    with pytest.raises(SearchDriverIneligible) as captured:
        driver.close()

    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED
    assert driver._poisoned is True
    operations = (
        lambda: driver.upsert((_resource("resource-a"),)),
        lambda: driver.delete(
            tenant_id="tenant-a",
            source_id="source-primary",
            resource_id="resource-a",
        ),
        lambda: driver.search(_query()),
    )
    for operation in operations:
        with pytest.raises(SearchDriverIneligible) as unavailable:
            operation()
        assert unavailable.value.reason is PlatformReason.SHUTDOWN_FAILED
    driver.cancel()
    driver.close()
    assert driver._closed is True


@pytest.mark.parametrize(
    "body_error",
    [
        KeyboardInterrupt(),
        SearchDriverIneligible(PlatformReason.CANCELLED, stage="test.body"),
    ],
)
def test_context_exit_preserves_body_control_flow_over_close_failure(
    body_error: BaseException,
) -> None:
    class CloseFailingConnection(sqlite3.Connection):
        close_attempts = 0

        def close(self) -> None:
            self.close_attempts += 1
            super().close()
            if self.close_attempts == 1:
                raise sqlite3.OperationalError("sensitive context close failure")

    driver = EmbeddedSearchDriver(":memory:")
    driver._connection.close()
    driver._connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=CloseFailingConnection,
    )
    captured: BaseException | None = None

    try:
        with driver:
            raise body_error
    except BaseException as error:
        captured = error

    assert captured is body_error
    assert driver._poisoned is True
    driver.close()
    assert driver._closed is True


def test_context_exit_prefers_shutdown_failure_over_ordinary_body_error() -> None:
    class CloseFailingConnection(sqlite3.Connection):
        close_attempts = 0

        def close(self) -> None:
            self.close_attempts += 1
            super().close()
            if self.close_attempts == 1:
                raise sqlite3.OperationalError("sensitive context close failure")

    driver = EmbeddedSearchDriver(":memory:")
    driver._connection.close()
    driver._connection = sqlite3.connect(
        ":memory:",
        check_same_thread=False,
        factory=CloseFailingConnection,
    )

    with pytest.raises(SearchDriverIneligible) as captured, driver:
        raise ValueError("sensitive body failure")

    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED
    assert "sensitive" not in str(captured.value)
    assert driver._poisoned is True
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


def test_active_non_sqlite_cancel_failure_is_sanitized() -> None:
    class RejectingInterruptConnection(sqlite3.Connection):
        def interrupt(self) -> None:
            raise RuntimeError("sensitive cancellation failure")

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
    assert "sensitive" not in str(captured.value)
    driver.close()
