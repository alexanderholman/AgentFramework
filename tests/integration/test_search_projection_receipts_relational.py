from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime
from threading import Event

import pytest

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseCursor,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    DatabaseTransaction,
)
from af.infrastructure.mariadb import MariaDBDriver
from af.infrastructure.mysql import MySQLDriver
from af.infrastructure.postgresql import PostgreSQLDriver
from af.messaging import (
    ProjectionCheckpoint,
    ProjectionCheckpointStore,
    ProjectionDecision,
)
from af.search.contracts import (
    ResourceEnvelope,
    ResourceKind,
    SearchFilter,
    SearchQuery,
)
from af.search.embedded import EmbeddedSearchDriver
from af.search.lifecycle import IndexBuild, ResourceIdentity
from af.search.projection_receipts import (
    LifecycleSearchProjectionTarget,
    SearchProjectionDelivery,
    SearchProjectionError,
    SearchProjectionTargetCheckpoint,
    SearchTombstone,
    TransactionalSearchProjector,
)

_NOW = datetime(2026, 7, 24, 18, tzinfo=UTC)
_PROBE_TABLE = "af_search_projector_conformance"


class _FailsOnceTarget:
    def __init__(self, target: EmbeddedSearchDriver) -> None:
        self._target = target
        self._fail = True

    @property
    def physical_name(self) -> str:
        return self._target.physical_name

    @property
    def generation(self) -> str:
        return self._target.generation

    @property
    def projection_version(self) -> int:
        return self._target.projection_version

    def projection_checkpoint(
        self,
        *,
        projection_name: str,
        source_name: str,
    ) -> SearchProjectionTargetCheckpoint | None:
        return self._target.projection_checkpoint(
            projection_name=projection_name,
            source_name=source_name,
        )

    def apply_projection(
        self,
        delivery: SearchProjectionDelivery,
        /,
        *,
        projection_name: str,
        expected: SearchProjectionTargetCheckpoint | None,
    ) -> bool:
        if self._fail:
            self._fail = False
            raise RuntimeError("private target credential")
        return self._target.apply_projection(
            delivery,
            projection_name=projection_name,
            expected=expected,
        )


class _ObservedCheckpointInsert:
    def __init__(self, inner: DatabaseTransaction, started: Event) -> None:
        self._inner = inner
        self._started = started

    def execute(self, statement: DatabaseStatement, /) -> int:
        if "INSERT INTO af_projection_checkpoints" in statement.sql:
            self._started.set()
        return self._inner.execute(statement)

    def query(self, statement: DatabaseStatement, /) -> tuple[DatabaseRow, ...]:
        return self._inner.query(statement)

    def stream(self, statement: DatabaseStatement, /) -> DatabaseCursor:
        return self._inner.stream(statement)

    def savepoint(self, name: str, /) -> AbstractContextManager[None]:
        return self._inner.savepoint(name)

    def mark_rollback_only(self) -> None:
        self._inner.mark_rollback_only()

    def claim_operation(self, name: str, /) -> bool:
        return self._inner.claim_operation(name)


def _resource(backend: str) -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=f"resource-{backend}",
        kind=ResourceKind.RELATIONAL,
        source_id=f"source-{backend}",
        authoritative_locator=f"af://source-{backend}/resource-{backend}",
        tenant_id="tenant-a",
        projection_version=2,
        acl=("group:research",),
        provenance=("sha256:" + "a" * 64,),
        text="original durable relational projection",
    )


def _query(text: str) -> SearchQuery:
    return SearchQuery(
        text=text,
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
        ),
    )


def _checkpoint(
    driver: PostgreSQLDriver | MySQLDriver | MariaDBDriver,
    *,
    projection_name: str,
    source_name: str,
) -> ProjectionCheckpoint | None:
    with driver.transaction(read_only=True) as transaction:
        return ProjectionCheckpointStore().load(
            transaction,
            projection_name=projection_name,
            source_name=source_name,
        )


def _exercise(
    driver: PostgreSQLDriver | MySQLDriver | MariaDBDriver,
    backend: str,
) -> None:
    projection_name = f"search.projector.{backend}"
    source_name = f"records.{backend}.partition-0"
    resource = _resource(backend)
    identity = ResourceIdentity.from_resource(resource)
    logical_name = f"catalog-{backend}"
    physical_name = f"{logical_name}-g00000002"
    with EmbeddedSearchDriver(
        ":memory:",
        physical_name=physical_name,
        projection_version=2,
    ) as search:
        target = LifecycleSearchProjectionTarget(
            IndexBuild(
                logical_name=logical_name,
                physical_name=physical_name,
                generation=2,
                projection_version=2,
                base_index=None,
                started_at=_NOW,
            ),
            _FailsOnceTarget(search),
        )
        projector = TransactionalSearchProjector(
            projection_name=projection_name,
            initial_position=1,
            source_owner=lambda _identity: source_name,
            target=target,
            clock=lambda: _NOW,
        )
        with driver.transaction() as transaction:
            projector.install(transaction)
            transaction.execute(
                DatabaseStatement(
                    f"""CREATE TABLE IF NOT EXISTS {_PROBE_TABLE} (
                        backend VARCHAR(32) PRIMARY KEY,
                        marker VARCHAR(32) NOT NULL
                    )"""
                )
            )
        with driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    """DELETE FROM af_inbox
                    WHERE consumer_name = :projection_name
                      AND source_name = :source_name""",
                    {
                        "projection_name": projector.checkpoint_name,
                        "source_name": source_name,
                    },
                )
            )
            transaction.execute(
                DatabaseStatement(
                    """DELETE FROM af_projection_checkpoints
                    WHERE projection_name = :projection_name
                      AND source_name = :source_name""",
                    {
                        "projection_name": projector.checkpoint_name,
                        "source_name": source_name,
                    },
                )
            )
            transaction.execute(
                DatabaseStatement(
                    f"DELETE FROM {_PROBE_TABLE} WHERE backend = :backend",
                    {"backend": backend},
                )
            )

        delivery = SearchProjectionDelivery(
            source_name=source_name,
            message_id=f"projector-{backend}-1",
            position=1,
            mutation=resource,
        )
        with driver.transaction() as transaction:
            with pytest.raises(SearchProjectionError) as captured:
                projector.apply(transaction, delivery)
            transaction.execute(
                DatabaseStatement(
                    f"""INSERT INTO {_PROBE_TABLE} (backend, marker)
                    VALUES (:backend, :marker)""",
                    {"backend": backend, "marker": "outer-committed"},
                )
            )

        assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
        assert "private" not in str(captured.value)
        assert not search.search(_query("original"))
        assert (
            _checkpoint(
                driver,
                projection_name=projector.checkpoint_name,
                source_name=source_name,
            )
            is None
        )
        with driver.transaction(read_only=True) as transaction:
            rows = transaction.query(
                DatabaseStatement(
                    f"""SELECT marker FROM {_PROBE_TABLE}
                    WHERE backend = :backend""",
                    {"backend": backend},
                    read_only=True,
                )
            )
        assert tuple(row["marker"] for row in rows) == ("outer-committed",)

        with (
            pytest.raises(RuntimeError, match="simulated process loss"),
            driver.transaction() as transaction,
        ):
            assert projector.apply(transaction, delivery) is ProjectionDecision.APPLY
            raise RuntimeError("simulated process loss")

        assert tuple(hit.resource_id for hit in search.search(_query("original"))) == (
            resource.resource_id,
        )
        assert (
            _checkpoint(
                driver,
                projection_name=projector.checkpoint_name,
                source_name=source_name,
            )
            is None
        )

        with driver.transaction() as transaction:
            assert projector.apply(transaction, delivery) is ProjectionDecision.APPLY

        altered = replace(
            resource,
            text="altered duplicate projection",
            provenance=("sha256:" + "b" * 64,),
        )
        with driver.transaction() as transaction:
            assert (
                projector.apply(
                    transaction,
                    replace(delivery, mutation=altered),
                )
                is ProjectionDecision.DUPLICATE
            )
        assert not search.search(_query("altered"))

        with driver.transaction() as transaction:
            assert (
                projector.apply(
                    transaction,
                    SearchProjectionDelivery(
                        source_name=source_name,
                        message_id=f"projector-{backend}-2",
                        position=2,
                        mutation=SearchTombstone(identity, 2),
                    ),
                )
                is ProjectionDecision.APPLY
            )

        assert not search.search(_query("original"))
        checkpoint = _checkpoint(
            driver,
            projection_name=projector.checkpoint_name,
            source_name=source_name,
        )
        assert checkpoint is not None
        assert checkpoint.position == 2
        assert checkpoint.message_id == f"projector-{backend}-2"

        with driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    """DELETE FROM af_inbox
                    WHERE consumer_name = :projection_name
                      AND source_name = :source_name""",
                    {
                        "projection_name": projector.checkpoint_name,
                        "source_name": source_name,
                    },
                )
            )
            transaction.execute(
                DatabaseStatement(
                    """DELETE FROM af_projection_checkpoints
                    WHERE projection_name = :projection_name
                      AND source_name = :source_name""",
                    {
                        "projection_name": projector.checkpoint_name,
                        "source_name": source_name,
                    },
                )
            )
            transaction.execute(
                DatabaseStatement(
                    f"DELETE FROM {_PROBE_TABLE} WHERE backend = :backend",
                    {"backend": backend},
                )
            )


def _exercise_concurrent_first_delivery(
    first: PostgreSQLDriver | MySQLDriver | MariaDBDriver,
    second: PostgreSQLDriver | MySQLDriver | MariaDBDriver,
    backend: str,
) -> None:
    projection_name = f"search.projector.{backend}.concurrent"
    source_name = f"records.{backend}.concurrent"
    logical_name = f"catalog-{backend}-concurrent"
    physical_name = f"{logical_name}-g00000003"
    resource = _resource(backend)
    delivery = SearchProjectionDelivery(
        source_name=source_name,
        message_id=f"projector-{backend}-concurrent-1",
        position=1,
        mutation=resource,
    )
    with EmbeddedSearchDriver(
        ":memory:",
        physical_name=physical_name,
        projection_version=2,
    ) as search:
        target = LifecycleSearchProjectionTarget(
            IndexBuild(
                logical_name=logical_name,
                physical_name=physical_name,
                generation=3,
                projection_version=2,
                base_index=None,
                started_at=_NOW,
            ),
            search,
        )
        projector = TransactionalSearchProjector(
            projection_name=projection_name,
            initial_position=1,
            source_owner=lambda _identity: source_name,
            target=target,
            clock=lambda: _NOW,
        )
        with first.transaction() as transaction:
            projector.install(transaction)
        with first.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    """DELETE FROM af_inbox
                    WHERE consumer_name = :projection_name
                      AND source_name = :source_name""",
                    {
                        "projection_name": projector.checkpoint_name,
                        "source_name": source_name,
                    },
                )
            )
            transaction.execute(
                DatabaseStatement(
                    """DELETE FROM af_projection_checkpoints
                    WHERE projection_name = :projection_name
                      AND source_name = :source_name""",
                    {
                        "projection_name": projector.checkpoint_name,
                        "source_name": source_name,
                    },
                )
            )

        insert_started = Event()

        def lose_optimistic_race() -> ProjectionDecision:
            with second.transaction() as transaction:
                return projector.apply(
                    _ObservedCheckpointInsert(transaction, insert_started),
                    delivery,
                )

        executor = ThreadPoolExecutor(max_workers=1)
        try:
            with first.transaction() as winner:
                assert projector.apply(winner, delivery) is ProjectionDecision.APPLY
                future = executor.submit(lose_optimistic_race)
                assert insert_started.wait(timeout=10)
            # Concurrent first writers can lose through a unique/CAS conflict or
            # InnoDB's deadlock victim rollback. Both require a fresh transaction.
            with pytest.raises(DatabaseOperationError) as captured:
                future.result(timeout=10)
        finally:
            executor.shutdown(wait=True, cancel_futures=True)

        # Diagnostic evidence must retain numeric vendor codes without printing SQL,
        # parameters or backend messages from protected exception chains.
        pending: list[BaseException] = [captured.value]
        seen: set[int] = set()
        vendor_codes: list[int] = []
        while pending and len(seen) < 16:
            error = pending.pop()
            if id(error) in seen:
                continue
            seen.add(id(error))
            if error.args and type(error.args[0]) is int:
                vendor_codes.append(error.args[0])
            for attribute in ("orig", "protected_cause", "__context__", "__cause__"):
                nested = getattr(error, attribute, None)
                if isinstance(nested, BaseException):
                    pending.append(nested)
        assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE, (
            f"sanitized vendor error codes: {sorted(set(vendor_codes))}"
        )
        with second.transaction() as retry:
            assert projector.apply(retry, delivery) is ProjectionDecision.DUPLICATE
        assert tuple(hit.resource_id for hit in search.search(_query("original"))) == (
            resource.resource_id,
        )
        with first.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    """DELETE FROM af_inbox
                    WHERE consumer_name = :projection_name
                      AND source_name = :source_name""",
                    {
                        "projection_name": projector.checkpoint_name,
                        "source_name": source_name,
                    },
                )
            )
            transaction.execute(
                DatabaseStatement(
                    """DELETE FROM af_projection_checkpoints
                    WHERE projection_name = :projection_name
                      AND source_name = :source_name""",
                    {
                        "projection_name": projector.checkpoint_name,
                        "source_name": source_name,
                    },
                )
            )


@pytest.mark.parametrize(
    ("backend", "environment", "factory"),
    [
        ("postgresql", "AF_POSTGRESQL_TEST_URL", PostgreSQLDriver),
        ("mysql", "AF_MYSQL_TEST_URL", MySQLDriver),
        ("mariadb", "AF_MARIADB_TEST_URL", MariaDBDriver),
    ],
)
def test_search_projector_replays_safely_on_real_relational_services(
    backend: str,
    environment: str,
    factory: type[PostgreSQLDriver] | type[MySQLDriver] | type[MariaDBDriver],
) -> None:
    url = os.environ.get(environment)
    if url is None:
        pytest.skip(f"{environment} is not configured")
    with factory(url) as driver:
        _exercise(driver, backend)


@pytest.mark.parametrize(
    ("backend", "environment", "factory"),
    [
        ("postgresql", "AF_POSTGRESQL_TEST_URL", PostgreSQLDriver),
        ("mysql", "AF_MYSQL_TEST_URL", MySQLDriver),
        ("mariadb", "AF_MARIADB_TEST_URL", MariaDBDriver),
    ],
)
def test_search_projector_concurrent_first_delivery_is_retryable(
    backend: str,
    environment: str,
    factory: type[PostgreSQLDriver] | type[MySQLDriver] | type[MariaDBDriver],
) -> None:
    url = os.environ.get(environment)
    if url is None:
        pytest.skip(f"{environment} is not configured")
    with factory(url) as first, factory(url) as second:
        _exercise_concurrent_first_delivery(first, second, backend)
