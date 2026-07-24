from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta
from itertools import repeat
from pathlib import Path
from types import TracebackType
from typing import Literal, cast
from unittest.mock import Mock

import pytest

from af.framework.container import ProviderRegistrar
from af.framework.database_manager import DatabaseManager
from af.framework.errors import PlatformReason
from af.framework.migration_provider import MigrationProvider
from af.framework.migrations import (
    Migration,
    MigrationDirection,
    MigrationIntegrityError,
    MigrationIrreversibleError,
    MigrationLockUnavailable,
    MigrationMode,
    MigrationProbe,
    MigrationRunner,
    MigrationVerificationError,
)
from af.framework.relational import (
    DatabaseCursor,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    DatabaseTransaction,
    UnsupportedDatabaseFeature,
)
from af.infrastructure.sqlite import SQLiteDriver

_NOW = datetime(2026, 7, 24, 8, tzinfo=UTC)


class MutableClock:
    def __init__(self, value: datetime = _NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def _statement(sql: str) -> DatabaseStatement:
    return DatabaseStatement(sql)


def _probe_table(name: str) -> MigrationProbe:
    return MigrationProbe(
        DatabaseStatement(
            f"SELECT name FROM sqlite_master WHERE type = 'table' AND name = '{name}'",
            read_only=True,
        ),
        (DatabaseRow(("name",), (name,)),),
    )


def _migration_one() -> Migration:
    return Migration(
        "20260724080000_create_widgets",
        (_statement("CREATE TABLE widgets (id INTEGER PRIMARY KEY, value TEXT NOT NULL)"),),
        (_statement("DROP TABLE widgets"),),
        (_probe_table("widgets"),),
    )


def _migration_two() -> Migration:
    return Migration(
        "20260724080100_create_gadgets",
        (_statement("CREATE TABLE gadgets (id INTEGER PRIMARY KEY, widget_id INTEGER)"),),
        (_statement("DROP TABLE gadgets"),),
        (_probe_table("gadgets"),),
    )


def _legacy_event_migration() -> Migration:
    return Migration(
        "20260724080200_seed_event",
        (_statement("INSERT INTO migration_events (value) VALUES (1)"),),
        (_statement("DELETE FROM migration_events WHERE value = 1"),),
        (
            MigrationProbe(
                DatabaseStatement(
                    "SELECT value FROM migration_events ORDER BY value",
                    read_only=True,
                ),
                (DatabaseRow(("value",), (1,)),),
            ),
        ),
    )


def _data_event_migration(
    *,
    accept_pre_mode_checksum: bool | None = None,
    up: tuple[DatabaseStatement, ...] | None = None,
) -> Migration:
    legacy = _legacy_event_migration()
    if accept_pre_mode_checksum is None:
        return Migration(
            legacy.migration_id,
            legacy.up if up is None else up,
            legacy.down,
            legacy.verify,
            mode=MigrationMode.DATA,
        )
    return Migration(
        legacy.migration_id,
        legacy.up if up is None else up,
        legacy.down,
        legacy.verify,
        mode=MigrationMode.DATA,
        accept_pre_mode_checksum=accept_pre_mode_checksum,
    )


def _runner(
    driver: SQLiteDriver,
    migrations: tuple[Migration, ...],
    *,
    owner: str = "test-runner",
    now: datetime = _NOW,
    lease: timedelta = timedelta(minutes=5),
) -> MigrationRunner:
    return MigrationRunner(
        driver,
        migrations,
        owner_id=owner,
        clock=lambda: now,
        lock_lease=lease,
    )


def _table_exists(driver: SQLiteDriver, name: str) -> bool:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = :name",
                {"name": name},
                read_only=True,
            )
        )
    return bool(rows)


def _ledger_rows(driver: SQLiteDriver) -> tuple[DatabaseRow, ...]:
    with driver.transaction(read_only=True) as transaction:
        return transaction.query(
            DatabaseStatement(
                "SELECT migration_id, checksum, batch, applied_order FROM af_migrations "
                "ORDER BY applied_order",
                read_only=True,
            )
        )


class GuardedCursor(Iterator[DatabaseRow]):
    def __init__(self, rows: Iterable[DatabaseRow], *, maximum_pulls: int) -> None:
        self._rows = iter(rows)
        self._maximum_pulls = maximum_pulls
        self.pulls = 0
        self.closed = False

    def __next__(self) -> DatabaseRow:
        if self.closed:
            raise StopIteration
        row = next(self._rows)
        self.pulls += 1
        if self.pulls > self._maximum_pulls:
            raise AssertionError("migration query consumed beyond its declared bound")
        return row

    def close(self) -> None:
        self.closed = True


class FailingCursor(Iterator[DatabaseRow]):
    def __init__(
        self,
        error: DatabaseOperationError | None = None,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self._error = error
        self._close_error = close_error
        self.closed = False

    def __next__(self) -> DatabaseRow:
        if self._error is None:
            raise StopIteration
        raise self._error

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


class StreamingTransaction:
    def __init__(self, cursors: list[DatabaseCursor]) -> None:
        self._cursors = cursors
        self._opened: list[DatabaseCursor] = []

    def execute(self, statement: DatabaseStatement, /) -> int:
        del statement
        raise AssertionError("streaming test transaction does not execute statements")

    def query(self, statement: DatabaseStatement, /) -> tuple[DatabaseRow, ...]:
        del statement
        raise AssertionError("migration reads must not materialize unbounded query results")

    def stream(self, statement: DatabaseStatement, /) -> DatabaseCursor:
        del statement
        if not self._cursors:
            raise AssertionError("unexpected migration stream")
        cursor = self._cursors.pop(0)
        self._opened.append(cursor)
        return cursor

    def savepoint(self, name: str, /) -> AbstractContextManager[None]:
        del name
        return nullcontext()

    def finish(self) -> None:
        for cursor in self._opened:
            cursor.close()


class StreamingTransactionContext(AbstractContextManager[DatabaseTransaction]):
    def __init__(self, transaction: StreamingTransaction) -> None:
        self._transaction = transaction

    def __enter__(self) -> DatabaseTransaction:
        return self._transaction

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        self._transaction.finish()
        return False


class StreamingDriver:
    def __init__(
        self,
        cursors: Iterable[DatabaseCursor] = (),
        *,
        features: frozenset[DatabaseFeature] = frozenset(
            {DatabaseFeature.TRANSACTIONS, DatabaseFeature.STREAMING}
        ),
    ) -> None:
        self._cursors = list(cursors)
        self._features = features
        self.required: list[DatabaseFeature] = []

    @property
    def features(self) -> frozenset[DatabaseFeature]:
        return self._features

    def supports(self, feature: DatabaseFeature, /) -> bool:
        return feature in self._features

    def require(self, feature: DatabaseFeature, /) -> None:
        self.required.append(feature)
        if not self.supports(feature):
            raise UnsupportedDatabaseFeature(feature)

    def transaction(
        self,
        *,
        read_only: bool = False,
    ) -> AbstractContextManager[DatabaseTransaction]:
        del read_only
        return StreamingTransactionContext(StreamingTransaction(self._cursors))

    def cancel(self) -> None:
        pass

    def close(self) -> None:
        pass


class NonTransactionalDDLSQLiteDriver(SQLiteDriver):
    """SQLite-backed test service that advertises only portable data guarantees."""

    @property
    def features(self) -> frozenset[DatabaseFeature]:
        return frozenset(
            {
                DatabaseFeature.TRANSACTIONS,
                DatabaseFeature.STREAMING,
            }
        )

    def supports(self, feature: DatabaseFeature, /) -> bool:
        return feature in self.features


def _applied_row(migration_id: str, checksum: str) -> DatabaseRow:
    return DatabaseRow(
        ("migration_id", "checksum", "batch", "applied_order", "applied_at"),
        (migration_id, checksum, 1, 1, "2026-07-24T08:00:00.000000Z"),
    )


def _record_applied(
    driver: SQLiteDriver,
    migrations: tuple[Migration, ...],
) -> None:
    with driver.transaction() as transaction:
        for applied_order, migration in enumerate(migrations, start=1):
            transaction.execute(
                DatabaseStatement(
                    "INSERT INTO af_migrations VALUES "
                    "(:id, :checksum, 1, :applied_order, :applied_at)",
                    {
                        "id": migration.migration_id,
                        "checksum": migration.checksum,
                        "applied_order": applied_order,
                        "applied_at": "2026-07-24T08:00:00.000000Z",
                    },
                )
            )


def _lock_rows(driver: SQLiteDriver) -> tuple[DatabaseRow, ...]:
    with driver.transaction(read_only=True) as transaction:
        return transaction.query(
            DatabaseStatement(
                "SELECT lock_name FROM af_migration_locks ORDER BY lock_name",
                read_only=True,
            )
        )


def _event_values(driver: SQLiteDriver) -> tuple[int, ...]:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                "SELECT value FROM migration_events ORDER BY value",
                read_only=True,
            )
        )
    return tuple(cast(int, row["value"]) for row in rows)


def test_migration_ledger_read_stops_after_catalogue_bound_and_closes_cursor() -> None:
    unexpected = _applied_row("20260724080000_unexpected", "0" * 64)
    cursor = GuardedCursor(repeat(unexpected), maximum_pulls=1)
    driver = StreamingDriver((cursor,))
    runner = MigrationRunner(
        driver,
        (),
        owner_id="bounded-ledger",
        clock=lambda: _NOW,
    )

    with pytest.raises(MigrationIntegrityError):
        runner.verify()

    assert cursor.pulls == 1
    assert cursor.closed
    assert driver.required == [
        DatabaseFeature.TRANSACTIONS,
        DatabaseFeature.STREAMING,
    ]


def test_migration_probe_stops_after_expected_bound_and_closes_cursor() -> None:
    probe = MigrationProbe(
        DatabaseStatement("SELECT value FROM probe", read_only=True),
        (),
    )
    migration = Migration(
        "20260724080000_bounded_probe",
        (DatabaseStatement("CREATE TABLE bounded_probe (value INTEGER)"),),
        verify=(probe,),
    )
    ledger = GuardedCursor(
        (_applied_row(migration.migration_id, migration.checksum),),
        maximum_pulls=2,
    )
    probe_rows = GuardedCursor(
        repeat(DatabaseRow(("value",), (1,))),
        maximum_pulls=1,
    )
    runner = MigrationRunner(
        StreamingDriver((ledger, probe_rows)),
        (migration,),
        owner_id="bounded-probe",
        clock=lambda: _NOW,
    )

    with pytest.raises(MigrationVerificationError) as captured:
        runner.verify()

    assert captured.value.migration_id == migration.migration_id
    assert ledger.pulls == 1
    assert ledger.closed
    assert probe_rows.pulls == 1
    assert probe_rows.closed


@pytest.mark.parametrize(
    "reason",
    [
        PlatformReason.CANCELLED,
        PlatformReason.SHUTDOWN_FAILED,
        PlatformReason.TRANSIENT_FAILURE,
    ],
)
def test_migration_probe_preserves_operational_failure_and_closes_cursor(
    reason: PlatformReason,
) -> None:
    probe = MigrationProbe(
        DatabaseStatement("SELECT value FROM probe", read_only=True),
        (),
    )
    migration = Migration(
        "20260724080000_failing_probe",
        (DatabaseStatement("CREATE TABLE failing_probe (value INTEGER)"),),
        verify=(probe,),
    )
    ledger = GuardedCursor(
        (_applied_row(migration.migration_id, migration.checksum),),
        maximum_pulls=2,
    )
    failure = DatabaseOperationError(
        reason=reason,
        stage="database.stream",
        protected_cause=RuntimeError("private database credential"),
    )
    probe_rows = FailingCursor(failure)
    runner = MigrationRunner(
        StreamingDriver((ledger, probe_rows)),
        (migration,),
        owner_id="failing-probe",
        clock=lambda: _NOW,
    )

    with pytest.raises(DatabaseOperationError) as captured:
        runner.verify()

    assert captured.value is failure
    assert "private database credential" not in str(captured.value)
    assert "private database credential" not in repr(captured.value)
    assert probe_rows.closed


def test_migration_probe_preserves_primary_failure_when_cursor_close_also_fails() -> None:
    probe = MigrationProbe(
        DatabaseStatement("SELECT value FROM probe", read_only=True),
        (),
    )
    migration = Migration(
        "20260724080000_dual_failure_probe",
        (DatabaseStatement("CREATE TABLE dual_failure_probe (value INTEGER)"),),
        verify=(probe,),
    )
    ledger = GuardedCursor(
        (_applied_row(migration.migration_id, migration.checksum),),
        maximum_pulls=2,
    )
    failure = DatabaseOperationError(
        reason=PlatformReason.CANCELLED,
        stage="database.stream",
        protected_cause=RuntimeError("private iteration detail"),
    )
    probe_rows = FailingCursor(
        failure,
        close_error=RuntimeError("private close detail"),
    )
    runner = MigrationRunner(
        StreamingDriver((ledger, probe_rows)),
        (migration,),
        owner_id="dual-failure-probe",
        clock=lambda: _NOW,
    )

    with pytest.raises(DatabaseOperationError) as captured:
        runner.verify()

    assert captured.value is failure
    assert "private iteration detail" not in str(captured.value)
    assert "private close detail" not in str(captured.value)
    assert probe_rows.closed


def test_migration_probe_late_cleanup_cancellation_precedes_transient_failure() -> None:
    probe = MigrationProbe(
        DatabaseStatement("SELECT value FROM probe", read_only=True),
        (),
    )
    migration = Migration(
        "20260724080000_cleanup_cancelled_probe",
        (DatabaseStatement("CREATE TABLE cleanup_cancelled_probe (value INTEGER)"),),
        verify=(probe,),
    )
    ledger = GuardedCursor(
        (_applied_row(migration.migration_id, migration.checksum),),
        maximum_pulls=2,
    )
    iteration_failure = DatabaseOperationError(
        reason=PlatformReason.TRANSIENT_FAILURE,
        stage="database.stream",
    )
    cleanup_cancellation = DatabaseOperationError(
        reason=PlatformReason.CANCELLED,
        stage="database.cursor-close",
    )
    probe_rows = FailingCursor(
        iteration_failure,
        close_error=cleanup_cancellation,
    )
    runner = MigrationRunner(
        StreamingDriver((ledger, probe_rows)),
        (migration,),
        owner_id="cleanup-cancelled-probe",
        clock=lambda: _NOW,
    )

    with pytest.raises(DatabaseOperationError) as captured:
        runner.verify()

    assert captured.value is cleanup_cancellation
    assert probe_rows.closed


def test_migration_probe_late_base_control_flow_precedes_operational_failure() -> None:
    probe = MigrationProbe(
        DatabaseStatement("SELECT value FROM probe", read_only=True),
        (),
    )
    migration = Migration(
        "20260724080000_cleanup_control_flow_probe",
        (DatabaseStatement("CREATE TABLE cleanup_control_flow_probe (value INTEGER)"),),
        verify=(probe,),
    )
    ledger = GuardedCursor(
        (_applied_row(migration.migration_id, migration.checksum),),
        maximum_pulls=2,
    )
    cleanup_control_flow = KeyboardInterrupt()
    probe_rows = FailingCursor(
        DatabaseOperationError(
            reason=PlatformReason.TRANSIENT_FAILURE,
            stage="database.stream",
        ),
        close_error=cleanup_control_flow,
    )
    runner = MigrationRunner(
        StreamingDriver((ledger, probe_rows)),
        (migration,),
        owner_id="cleanup-control-flow-probe",
        clock=lambda: _NOW,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        runner.verify()

    assert captured.value is cleanup_control_flow
    assert probe_rows.closed


def test_migration_probe_sanitizes_standalone_cursor_close_failure() -> None:
    probe = MigrationProbe(
        DatabaseStatement("SELECT value FROM probe", read_only=True),
        (),
    )
    migration = Migration(
        "20260724080000_close_failure_probe",
        (DatabaseStatement("CREATE TABLE close_failure_probe (value INTEGER)"),),
        verify=(probe,),
    )
    ledger = GuardedCursor(
        (_applied_row(migration.migration_id, migration.checksum),),
        maximum_pulls=2,
    )
    close_failure = RuntimeError("private close detail")
    probe_rows = FailingCursor(close_error=close_failure)
    runner = MigrationRunner(
        StreamingDriver((ledger, probe_rows)),
        (migration,),
        owner_id="close-failure-probe",
        clock=lambda: _NOW,
    )

    with pytest.raises(DatabaseOperationError) as captured:
        runner.verify()

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "database.cursor-close"
    assert captured.value.protected_cause is close_failure
    assert "private close detail" not in str(captured.value)
    assert "private close detail" not in repr(captured.value)
    assert probe_rows.closed


def test_migration_runner_requires_streaming_before_database_work() -> None:
    driver = StreamingDriver(features=frozenset({DatabaseFeature.TRANSACTIONS}))

    with pytest.raises(UnsupportedDatabaseFeature) as captured:
        MigrationRunner(
            driver,
            (),
            owner_id="missing-streaming",
            clock=lambda: _NOW,
        )

    assert captured.value.feature is DatabaseFeature.STREAMING
    assert driver.required == [
        DatabaseFeature.TRANSACTIONS,
        DatabaseFeature.STREAMING,
    ]


def test_migration_provider_requires_streaming_from_named_connection() -> None:
    driver = StreamingDriver()
    manager = Mock(spec=DatabaseManager)
    manager.connection.return_value = driver
    resolver = Mock()
    resolver.resolve.return_value = manager
    registrar = Mock(spec=ProviderRegistrar)
    provider = MigrationProvider(
        migrations=(),
        connection="migration-primary",
        owner_id="provider-streaming",
        clock=lambda: _NOW,
    )

    provider.register(registrar)
    factory = registrar.bind.call_args.args[1]
    runner = factory(resolver)

    assert isinstance(runner, MigrationRunner)
    manager.connection.assert_called_once_with(
        "migration-primary",
        requires=(
            DatabaseFeature.TRANSACTIONS,
            DatabaseFeature.STREAMING,
        ),
    )


def test_default_schema_mode_preserves_the_legacy_checksum() -> None:
    migration = _migration_one()

    assert migration.mode is MigrationMode.SCHEMA
    assert migration.checksum == "79d156748a6e07e1d20c0e86002665bcc2ae87f7e96cbf0c4e57b197914940a0"


def test_explicit_data_mode_changes_the_checksum_identity() -> None:
    legacy = _migration_one()
    data = Migration(
        legacy.migration_id,
        legacy.up,
        legacy.down,
        legacy.verify,
        mode=MigrationMode.DATA,
    )

    assert data.checksum == "c7747d94cdc5f4b2c9dc148b4f4b2c29eaca01ff984d42cdc738447ac481dbe3"
    assert data.checksum != legacy.checksum


def test_data_mode_rejects_an_applied_pre_mode_checksum_by_default() -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    legacy = _legacy_event_migration()
    data = _data_event_migration()
    runner = MigrationRunner(
        driver,
        (data,),
        owner_id="pre-mode-default",
        clock=lambda: _NOW,
    )
    runner.bootstrap()
    _record_applied(driver, (legacy,))

    with pytest.raises(MigrationIntegrityError):
        runner.plan_up()

    driver.close()


def test_data_mode_opt_in_accepts_matching_pre_mode_checksum_for_verify_and_down() -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    legacy = _legacy_event_migration()
    data = _data_event_migration(accept_pre_mode_checksum=True)
    runner = MigrationRunner(
        driver,
        (data,),
        owner_id="pre-mode-accepted",
        clock=lambda: _NOW,
    )
    runner.bootstrap()
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement("CREATE TABLE migration_events (value INTEGER PRIMARY KEY)")
        )
        transaction.execute(data.up[0])
    _record_applied(driver, (legacy,))

    assert not driver.supports(DatabaseFeature.TRANSACTIONAL_DDL)
    assert (
        data.migration_id,
        data.up,
        data.down,
        data.verify,
    ) == (
        legacy.migration_id,
        legacy.up,
        legacy.down,
        legacy.verify,
    )
    assert runner.plan_up().empty
    assert runner.verify() == (data.migration_id,)
    assert runner.apply_down().migration_ids == (data.migration_id,)
    assert _event_values(driver) == ()
    assert _ledger_rows(driver) == ()
    driver.close()


def test_data_mode_opt_in_still_rejects_pre_mode_content_drift() -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    legacy = _legacy_event_migration()
    drifted = _data_event_migration(
        accept_pre_mode_checksum=True,
        up=(_statement("INSERT INTO migration_events (value) VALUES (2)"),),
    )
    runner = MigrationRunner(
        driver,
        (drifted,),
        owner_id="pre-mode-drift",
        clock=lambda: _NOW,
    )
    runner.bootstrap()
    _record_applied(driver, (legacy,))

    with pytest.raises(MigrationIntegrityError):
        runner.plan_up()

    driver.close()


def test_fresh_data_mode_opt_in_records_the_current_checksum() -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    legacy = _legacy_event_migration()
    data = _data_event_migration(accept_pre_mode_checksum=True)
    runner = MigrationRunner(
        driver,
        (data,),
        owner_id="pre-mode-fresh",
        clock=lambda: _NOW,
    )
    runner.bootstrap()
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement("CREATE TABLE migration_events (value INTEGER PRIMARY KEY)")
        )

    assert runner.apply_up().migration_ids == (data.migration_id,)

    rows = _ledger_rows(driver)
    assert len(rows) == 1
    assert data.checksum == _data_event_migration().checksum
    assert rows[0]["checksum"] == data.checksum
    assert rows[0]["checksum"] != legacy.checksum
    driver.close()


def test_pre_mode_checksum_opt_in_is_data_only_and_strictly_boolean() -> None:
    legacy = _legacy_event_migration()

    with pytest.raises(ValueError, match="pre-mode checksum"):
        Migration(
            legacy.migration_id,
            legacy.up,
            legacy.down,
            legacy.verify,
            accept_pre_mode_checksum=True,
        )
    with pytest.raises(ValueError, match="pre-mode checksum"):
        Migration(
            legacy.migration_id,
            legacy.up,
            legacy.down,
            legacy.verify,
            mode=MigrationMode.DATA,
            accept_pre_mode_checksum=cast(bool, 1),
        )


def test_migration_repr_shows_policy_without_pre_mode_checksum_detail() -> None:
    legacy = _legacy_event_migration()
    data = _data_event_migration(accept_pre_mode_checksum=True)

    rendered = repr(data)

    assert "accept_pre_mode_checksum=True" in rendered
    assert data.checksum in rendered
    assert legacy.checksum not in rendered


def test_apply_up_preflights_the_entire_mixed_plan_before_mutation() -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement("CREATE TABLE migration_events (value INTEGER PRIMARY KEY)")
        )
    data = Migration(
        "20260724080000_seed_event",
        (_statement("INSERT INTO migration_events (value) VALUES (1)"),),
        (_statement("DELETE FROM migration_events WHERE value = 1"),),
        mode=MigrationMode.DATA,
    )
    schema = Migration(
        "20260724080100_create_unsupported_schema",
        (_statement("CREATE TABLE unsupported_schema (id INTEGER PRIMARY KEY)"),),
        (_statement("DROP TABLE unsupported_schema"),),
    )
    runner = MigrationRunner(
        driver,
        (data, schema),
        owner_id="mixed-plan",
        clock=lambda: _NOW,
    )
    runner.bootstrap()

    with pytest.raises(UnsupportedDatabaseFeature) as captured:
        runner.apply_up()

    assert captured.value.feature is DatabaseFeature.TRANSACTIONAL_DDL
    assert _event_values(driver) == ()
    assert not _table_exists(driver, "unsupported_schema")
    assert _ledger_rows(driver) == ()
    driver.close()


@pytest.mark.parametrize("dry_run", [False, True])
def test_apply_down_preflights_the_entire_plan_before_mutation(dry_run: bool) -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    schema = Migration(
        "20260724080000_create_unsupported_schema",
        (_statement("CREATE TABLE unsupported_schema (id INTEGER PRIMARY KEY)"),),
        (_statement("DROP TABLE unsupported_schema"),),
    )
    data = Migration(
        "20260724080100_seed_event",
        (_statement("INSERT INTO migration_events (value) VALUES (1)"),),
        (_statement("DELETE FROM migration_events WHERE value = 1"),),
        mode=MigrationMode.DATA,
    )
    runner = MigrationRunner(
        driver,
        (schema, data),
        owner_id="down-mixed-plan",
        clock=lambda: _NOW,
    )
    runner.bootstrap()
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement("CREATE TABLE migration_events (value INTEGER PRIMARY KEY)")
        )
        transaction.execute(_statement("INSERT INTO migration_events (value) VALUES (1)"))
        transaction.execute(_statement("CREATE TABLE unsupported_schema (id INTEGER PRIMARY KEY)"))
    _record_applied(driver, (schema, data))

    with pytest.raises(UnsupportedDatabaseFeature) as captured:
        runner.apply_down(steps=2, dry_run=dry_run)

    assert captured.value.feature is DatabaseFeature.TRANSACTIONAL_DDL
    assert _event_values(driver) == (1,)
    assert _table_exists(driver, "unsupported_schema")
    assert tuple(row["migration_id"] for row in _ledger_rows(driver)) == (
        schema.migration_id,
        data.migration_id,
    )
    driver.close()


@pytest.mark.parametrize("dry_run", [False, True])
def test_apply_down_reports_irreversibility_before_capability_failure(
    dry_run: bool,
) -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    irreversible = Migration(
        "20260724080000_irreversible_event",
        (_statement("INSERT INTO migration_events (value) VALUES (1)"),),
        mode=MigrationMode.DATA,
    )
    schema = Migration(
        "20260724080100_create_unsupported_schema",
        (_statement("CREATE TABLE unsupported_schema (id INTEGER PRIMARY KEY)"),),
        (_statement("DROP TABLE unsupported_schema"),),
    )
    runner = MigrationRunner(
        driver,
        (irreversible, schema),
        owner_id="down-precedence",
        clock=lambda: _NOW,
    )
    runner.bootstrap()
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement("CREATE TABLE migration_events (value INTEGER PRIMARY KEY)")
        )
        transaction.execute(_statement("INSERT INTO migration_events (value) VALUES (1)"))
        transaction.execute(_statement("CREATE TABLE unsupported_schema (id INTEGER PRIMARY KEY)"))
    _record_applied(driver, (irreversible, schema))

    with pytest.raises(MigrationIrreversibleError) as captured:
        runner.apply_down(steps=2, dry_run=dry_run)

    assert captured.value.migration_id == irreversible.migration_id
    assert _event_values(driver) == (1,)
    assert _table_exists(driver, "unsupported_schema")
    assert len(_ledger_rows(driver)) == 2
    driver.close()


def test_unsupported_schema_dry_run_rejects_without_mutating_state() -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    migration = Migration(
        "20260724080000_create_unsupported_schema",
        (_statement("CREATE TABLE unsupported_schema (id INTEGER PRIMARY KEY)"),),
        (_statement("DROP TABLE unsupported_schema"),),
    )
    runner = MigrationRunner(
        driver,
        (migration,),
        owner_id="schema-dry-run",
        clock=lambda: _NOW,
    )
    runner.bootstrap()

    with pytest.raises(UnsupportedDatabaseFeature) as captured:
        runner.apply_up(dry_run=True)

    assert captured.value.feature is DatabaseFeature.TRANSACTIONAL_DDL
    assert runner.plan_up().migration_ids == (migration.migration_id,)
    assert not _table_exists(driver, "unsupported_schema")
    assert _ledger_rows(driver) == ()
    assert _lock_rows(driver) == ()
    driver.close()


def test_empty_plans_do_not_require_transactional_ddl() -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    runner = MigrationRunner(
        driver,
        (),
        owner_id="empty-plan",
        clock=lambda: _NOW,
    )
    runner.bootstrap()

    assert runner.apply_up().empty
    assert runner.apply_up(dry_run=True).empty
    assert runner.apply_down().empty
    assert runner.apply_down(dry_run=True).empty
    assert _ledger_rows(driver) == ()
    driver.close()


def test_data_only_migration_succeeds_without_transactional_ddl() -> None:
    driver = NonTransactionalDDLSQLiteDriver(":memory:")
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement("CREATE TABLE migration_events (value INTEGER PRIMARY KEY)")
        )
    migration = Migration(
        "20260724080000_seed_event",
        (_statement("INSERT INTO migration_events (value) VALUES (1)"),),
        (_statement("DELETE FROM migration_events WHERE value = 1"),),
        mode=MigrationMode.DATA,
    )
    runner = MigrationRunner(
        driver,
        (migration,),
        owner_id="data-only",
        clock=lambda: _NOW,
    )
    runner.bootstrap()

    assert runner.apply_up().migration_ids == (migration.migration_id,)
    assert _event_values(driver) == (1,)
    assert tuple(row["migration_id"] for row in _ledger_rows(driver)) == (migration.migration_id,)

    assert runner.apply_down().migration_ids == (migration.migration_id,)
    assert _event_values(driver) == ()
    assert _ledger_rows(driver) == ()
    driver.close()


def test_bootstrap_is_idempotent_and_fresh_plan_is_deterministic() -> None:
    driver = SQLiteDriver(":memory:")
    runner = _runner(driver, (_migration_two(), _migration_one()))

    runner.bootstrap()
    runner.bootstrap()
    plan = runner.plan_up()

    assert plan.direction is MigrationDirection.UP
    assert plan.migration_ids == (
        "20260724080000_create_widgets",
        "20260724080100_create_gadgets",
    )
    assert not plan.empty
    assert runner.migrations == (_migration_one(), _migration_two())
    driver.close()


def test_dry_run_does_not_change_schema_or_ledger_after_bootstrap() -> None:
    driver = SQLiteDriver(":memory:")
    runner = _runner(driver, (_migration_one(),))
    runner.bootstrap()

    plan = runner.apply_up(dry_run=True)

    assert plan.migration_ids == ("20260724080000_create_widgets",)
    assert not _table_exists(driver, "widgets")
    assert _ledger_rows(driver) == ()
    driver.close()


def test_fresh_install_applies_verifies_and_records_exact_ledger() -> None:
    driver = SQLiteDriver(":memory:")
    migration = _migration_one()
    runner = _runner(driver, (migration,))
    runner.bootstrap()

    plan = runner.apply_up()

    assert plan.migration_ids == (migration.migration_id,)
    assert runner.verify() == (migration.migration_id,)
    assert _table_exists(driver, "widgets")
    rows = _ledger_rows(driver)
    assert len(rows) == 1
    assert rows[0]["migration_id"] == migration.migration_id
    assert rows[0]["checksum"] == migration.checksum
    assert rows[0]["batch"] == 1
    assert rows[0]["applied_order"] == 1
    assert runner.plan_up().empty
    driver.close()


def test_supported_upgrade_applies_only_new_catalogue_suffix() -> None:
    driver = SQLiteDriver(":memory:")
    first = _migration_one()
    second = _migration_two()
    initial = _runner(driver, (first,))
    initial.bootstrap()
    initial.apply_up()

    upgraded = _runner(driver, (first, second), owner="upgrade-runner")
    plan = upgraded.apply_up()

    assert plan.migration_ids == (second.migration_id,)
    rows = _ledger_rows(driver)
    assert tuple(row["migration_id"] for row in rows) == (
        first.migration_id,
        second.migration_id,
    )
    assert tuple(row["batch"] for row in rows) == (1, 2)
    assert tuple(row["applied_order"] for row in rows) == (1, 2)
    driver.close()


def test_checksum_drift_and_non_prefix_ledger_fail_closed() -> None:
    driver = SQLiteDriver(":memory:")
    first = _migration_one()
    runner = _runner(driver, (first,))
    runner.bootstrap()
    runner.apply_up()

    drifted = Migration(
        first.migration_id,
        (_statement("CREATE TABLE widgets (id INTEGER PRIMARY KEY, changed TEXT)"),),
        first.down,
        first.verify,
    )
    with pytest.raises(MigrationIntegrityError) as checksum:
        _runner(driver, (drifted,), owner="drift-check").plan_up()
    assert checksum.value.reason is PlatformReason.INTEGRITY_FAILURE

    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("DELETE FROM af_migrations"))
        transaction.execute(
            DatabaseStatement(
                "INSERT INTO af_migrations VALUES (:id, :checksum, 1, 1, :applied_at)",
                {
                    "id": "20260724080100_create_gadgets",
                    "checksum": _migration_two().checksum,
                    "applied_at": "2026-07-24T08:00:00.000000Z",
                },
            )
        )
    with pytest.raises(MigrationIntegrityError):
        _runner(driver, (first, _migration_two()), owner="prefix-check").plan_up()
    driver.close()


def test_interrupted_migration_rolls_back_schema_and_ledger_on_sqlite() -> None:
    driver = SQLiteDriver(":memory:")
    broken = Migration(
        "20260724080200_broken_change",
        (
            _statement("CREATE TABLE partial_change (id INTEGER)"),
            _statement("THIS IS NOT VALID SQL"),
        ),
    )
    runner = _runner(driver, (broken,))
    runner.bootstrap()

    with pytest.raises(DatabaseOperationError):
        runner.apply_up()

    assert not _table_exists(driver, "partial_change")
    assert _ledger_rows(driver) == ()
    with runner.lock():
        pass
    driver.close()


def test_down_plan_dry_run_and_apply_reverse_latest_migration() -> None:
    driver = SQLiteDriver(":memory:")
    first = _migration_one()
    second = _migration_two()
    runner = _runner(driver, (first, second))
    runner.bootstrap()
    runner.apply_up()

    dry_plan = runner.apply_down(dry_run=True)
    assert dry_plan.direction is MigrationDirection.DOWN
    assert dry_plan.migration_ids == (second.migration_id,)
    assert _table_exists(driver, "gadgets")

    applied = runner.apply_down()
    assert applied == dry_plan
    assert not _table_exists(driver, "gadgets")
    assert _table_exists(driver, "widgets")
    assert runner.verify() == (first.migration_id,)
    driver.close()


def test_irreversible_down_fails_before_mutating_ledger() -> None:
    driver = SQLiteDriver(":memory:")
    irreversible = Migration(
        "20260724080300_irreversible_change",
        (_statement("CREATE TABLE irreversible (id INTEGER)"),),
    )
    runner = _runner(driver, (irreversible,))
    runner.bootstrap()
    runner.apply_up()

    with pytest.raises(MigrationIrreversibleError) as captured:
        runner.apply_down()

    assert captured.value.migration_id == irreversible.migration_id
    assert _table_exists(driver, "irreversible")
    assert len(_ledger_rows(driver)) == 1
    driver.close()


def test_verify_detects_schema_tampering_without_exposing_sql() -> None:
    driver = SQLiteDriver(":memory:")
    migration = _migration_one()
    runner = _runner(driver, (migration,))
    runner.bootstrap()
    runner.apply_up()
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("DROP TABLE widgets"))

    with pytest.raises(MigrationVerificationError) as captured:
        runner.verify()

    assert captured.value.migration_id == migration.migration_id
    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert "sqlite_master" not in str(captured.value)
    assert "sqlite_master" not in repr(captured.value)
    driver.close()


def test_database_lease_prevents_concurrent_runner_and_releases_cleanly(tmp_path: Path) -> None:
    database = tmp_path / "migration-lock.sqlite3"
    first_driver = SQLiteDriver(database)
    second_driver = SQLiteDriver(database)
    first = _runner(first_driver, (), owner="runner-a")
    second = _runner(second_driver, (), owner="runner-b")
    first.bootstrap()

    with first.lock():
        with pytest.raises(MigrationLockUnavailable) as captured, second.lock():
            pass
        assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE

    with second.lock():
        pass
    first_driver.close()
    second_driver.close()


def test_expired_database_lease_is_reclaimed(tmp_path: Path) -> None:
    database = tmp_path / "migration-expiry.sqlite3"
    first_driver = SQLiteDriver(database)
    first = _runner(first_driver, (), owner="runner-a")
    first.bootstrap()
    with first_driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                "INSERT INTO af_migration_locks VALUES (:name, :owner, :acquired, :expires)",
                {
                    "name": "schema",
                    "owner": "expired-owner",
                    "acquired": "2026-07-24T07:00:00.000000Z",
                    "expires": "2026-07-24T07:01:00.000000Z",
                },
            )
        )

    second_driver = SQLiteDriver(database)
    second = _runner(second_driver, (), owner="runner-b", now=_NOW)
    with second.lock():
        with second_driver.transaction(read_only=True) as transaction:
            rows = transaction.query(
                DatabaseStatement(
                    "SELECT owner_id FROM af_migration_locks WHERE lock_name = 'schema'",
                    read_only=True,
                )
            )
        assert rows[0]["owner_id"] == "runner-b"

    first_driver.close()
    second_driver.close()


def test_down_preflights_all_selected_migrations_before_mutation() -> None:
    driver = SQLiteDriver(":memory:")
    irreversible = Migration(
        "20260724080000_irreversible_base",
        (_statement("CREATE TABLE irreversible_base (id INTEGER)"),),
    )
    reversible = Migration(
        "20260724080100_reversible_latest",
        (_statement("CREATE TABLE reversible_latest (id INTEGER)"),),
        (_statement("DROP TABLE reversible_latest"),),
    )
    runner = _runner(driver, (irreversible, reversible))
    runner.bootstrap()
    runner.apply_up()

    with pytest.raises(MigrationIrreversibleError):
        runner.apply_down(steps=2)

    assert _table_exists(driver, "irreversible_base")
    assert _table_exists(driver, "reversible_latest")
    assert len(_ledger_rows(driver)) == 2
    driver.close()


def test_migration_catalogue_snapshot_and_probe_rows_are_bounded() -> None:
    driver = SQLiteDriver(":memory:")

    def endless_catalogue() -> Iterator[Migration]:
        while True:
            yield _migration_one()

    with pytest.raises(ValueError, match="catalogue"):
        MigrationRunner(
            driver,
            endless_catalogue(),
            owner_id="bounded-runner",
            clock=lambda: _NOW,
        )
    rows = tuple(DatabaseRow(("value",), (index,)) for index in range(1_001))
    with pytest.raises(ValueError, match="bounded exact tuple"):
        MigrationProbe(DatabaseStatement("SELECT 1", read_only=True), rows)
    driver.close()


def test_migration_contracts_reject_ambiguous_or_unbounded_inputs() -> None:
    write = _statement("CREATE TABLE valid_table (id INTEGER)")
    with pytest.raises(ValueError, match="migration mode"):
        Migration(
            "20260724080300_invalid_mode",
            (write,),
            mode=cast(MigrationMode, "data"),
        )
    with pytest.raises(ValueError, match="canonical timestamped"):
        Migration("not-canonical", (write,))
    with pytest.raises(ValueError, match="up statements"):
        Migration("20260724080400_invalid_up", ())
    with pytest.raises(ValueError, match="up statements"):
        Migration(
            "20260724080400_invalid_up",
            (DatabaseStatement("SELECT 1", read_only=True),),
        )
    with pytest.raises(ValueError, match="cannot contain parameters"):
        MigrationProbe(
            DatabaseStatement("SELECT :value", {"value": 1}, read_only=True),
            (),
        )
    with pytest.raises(ValueError, match="read-only"):
        MigrationProbe(write, ())

    driver = SQLiteDriver(":memory:")
    runner = _runner(driver, (_migration_one(),))
    runner.bootstrap()
    with pytest.raises(ValueError, match="plan limit"):
        runner.plan_up(limit=cast(int, True))
    with pytest.raises(ValueError, match="down steps"):
        runner.plan_down(steps=0)
    with pytest.raises(ValueError, match="dry-run"):
        runner.apply_up(dry_run=cast(bool, 1))
    with pytest.raises(ValueError, match="owner id"):
        MigrationRunner(driver, (), owner_id="INVALID OWNER", clock=lambda: _NOW)
    with pytest.raises(ValueError, match="lock lease"):
        MigrationRunner(
            driver,
            (),
            owner_id="valid-owner",
            clock=lambda: _NOW,
            lock_lease=timedelta(0),
        )
    driver.close()


def test_migration_lease_renewal_extends_exclusion_window(tmp_path: Path) -> None:
    database = tmp_path / "migration-renewal.sqlite3"
    clock = MutableClock()
    first_driver = SQLiteDriver(database)
    second_driver = SQLiteDriver(database)
    first = MigrationRunner(
        first_driver,
        (),
        owner_id="runner-a",
        clock=clock,
        lock_lease=timedelta(minutes=1),
    )
    second = MigrationRunner(
        second_driver,
        (),
        owner_id="runner-b",
        clock=clock,
        lock_lease=timedelta(minutes=1),
    )
    first.bootstrap()

    with first.lock() as lease:
        clock.advance(timedelta(seconds=30))
        lease.renew()
        clock.advance(timedelta(seconds=40))
        with pytest.raises(MigrationLockUnavailable), second.lock():
            pass

    with second.lock():
        pass
    first_driver.close()
    second_driver.close()


def test_reused_migration_owner_cannot_renew_or_release_reclaimed_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "migration-fencing.sqlite3"
    clock = MutableClock()
    first_driver = SQLiteDriver(database)
    second_driver = SQLiteDriver(database)
    first = MigrationRunner(
        first_driver,
        (),
        owner_id="shared-owner",
        clock=clock,
        lock_lease=timedelta(minutes=1),
    )
    reincarnated = MigrationRunner(
        second_driver,
        (),
        owner_id="shared-owner",
        clock=clock,
        lock_lease=timedelta(minutes=1),
    )
    first.bootstrap()

    with first.lock() as stale:
        clock.advance(timedelta(minutes=2))
        with reincarnated.lock() as current:
            with pytest.raises(MigrationLockUnavailable):
                stale.renew()
            clock.advance(timedelta(seconds=1))
            current.renew()

    with reincarnated.lock():
        pass
    first_driver.close()
    second_driver.close()


def test_migration_lease_renewal_requires_ownership_and_monotonic_time() -> None:
    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    runner = MigrationRunner(
        driver,
        (),
        owner_id="monotonic-runner",
        clock=clock,
        lock_lease=timedelta(minutes=1),
    )
    runner.bootstrap()
    lease = runner.lock()

    with pytest.raises(MigrationLockUnavailable):
        lease.renew()
    with lease:
        clock.advance(timedelta(microseconds=-1))
        with pytest.raises(MigrationIntegrityError):
            lease.renew()

    driver.close()
