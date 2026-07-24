from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from af.framework.errors import PlatformReason
from af.framework.migrations import (
    Migration,
    MigrationDirection,
    MigrationIntegrityError,
    MigrationIrreversibleError,
    MigrationLockUnavailable,
    MigrationProbe,
    MigrationRunner,
    MigrationVerificationError,
)
from af.framework.relational import (
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
)
from af.infrastructure.sqlite import SQLiteDriver

_NOW = datetime(2026, 7, 24, 8, tzinfo=UTC)


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

    def endless_catalogue():
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
