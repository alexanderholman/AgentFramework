from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from af.framework.migrations import (
    Migration,
    MigrationDirection,
    MigrationIntegrityError,
    MigrationLockUnavailable,
    MigrationProbe,
    MigrationRunner,
)
from af.framework.relational import (
    DatabaseDriver,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
)
from af.infrastructure.mariadb import MariaDBDriver
from af.infrastructure.mysql import MySQLDriver
from af.infrastructure.postgresql import PostgreSQLDriver

_NOW = datetime(2026, 7, 24, 9, tzinfo=UTC)
_TABLE = "af_migration_conformance"
_CASES: tuple[
    tuple[
        str,
        str,
        Callable[[str], DatabaseDriver],
    ],
    ...,
] = (
    ("postgresql", "AF_POSTGRESQL_TEST_URL", PostgreSQLDriver),
    ("mysql", "AF_MYSQL_TEST_URL", MySQLDriver),
    ("mariadb", "AF_MARIADB_TEST_URL", MariaDBDriver),
)


def _statement(sql: str) -> DatabaseStatement:
    return DatabaseStatement(sql)


def _probe(sql: str, rows: tuple[DatabaseRow, ...]) -> MigrationProbe:
    return MigrationProbe(DatabaseStatement(sql, read_only=True), rows)


def _create_table() -> Migration:
    return Migration(
        "20260724090000_create_conformance",
        (
            _statement(
                f"""CREATE TABLE {_TABLE} (
                    id INTEGER PRIMARY KEY,
                    value VARCHAR(64) NOT NULL
                )"""
            ),
        ),
        (_statement(f"DROP TABLE {_TABLE}"),),
        (_probe(f"SELECT value FROM {_TABLE} WHERE 1 = 0", ()),),
    )


def _seed_table() -> Migration:
    return Migration(
        "20260724090100_seed_conformance",
        (_statement(f"INSERT INTO {_TABLE} (id, value) VALUES (1, 'base')"),),
        (_statement(f"DELETE FROM {_TABLE} WHERE id = 1"),),
        (
            _probe(
                f"SELECT value FROM {_TABLE} WHERE id = 1",
                (DatabaseRow(("value",), ("base",)),),
            ),
        ),
    )


def _broken_change() -> Migration:
    return Migration(
        "20260724090200_broken_conformance",
        (
            _statement(f"INSERT INTO {_TABLE} (id, value) VALUES (2, 'partial')"),
            _statement("THIS IS NOT VALID SQL"),
        ),
    )


def _runner(
    driver: DatabaseDriver,
    migrations: tuple[Migration, ...],
    *,
    owner: str,
    now: datetime = _NOW,
) -> MigrationRunner:
    return MigrationRunner(
        driver,
        migrations,
        owner_id=owner,
        clock=lambda: now,
        lock_lease=timedelta(minutes=1),
    )


def _url(environment: str) -> str:
    value = os.environ.get(environment)
    if value is None:
        pytest.skip(f"{environment} is not configured")
    return value


def _reset(driver: DatabaseDriver) -> None:
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement(f"DROP TABLE IF EXISTS {_TABLE}"))
        transaction.execute(DatabaseStatement("DROP TABLE IF EXISTS af_migrations"))
        transaction.execute(DatabaseStatement("DROP TABLE IF EXISTS af_migration_locks"))


def _ledger_ids(driver: DatabaseDriver) -> tuple[object, ...]:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                "SELECT migration_id FROM af_migrations ORDER BY applied_order",
                read_only=True,
            )
        )
    return tuple(row["migration_id"] for row in rows)


def _values(driver: DatabaseDriver) -> tuple[object, ...]:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                f"SELECT value FROM {_TABLE} ORDER BY id",
                read_only=True,
            )
        )
    return tuple(row["value"] for row in rows)


@pytest.mark.parametrize(("backend", "environment", "factory"), _CASES)
def test_migration_lifecycle_passes_real_relational_services(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
) -> None:
    with factory(_url(environment)) as driver:
        _reset(driver)
        first = _create_table()
        second = _seed_table()
        initial = _runner(driver, (first,), owner=f"{backend}-initial")
        initial.bootstrap()
        initial.bootstrap()

        dry_run = initial.apply_up(dry_run=True)
        assert dry_run.direction is MigrationDirection.UP
        assert dry_run.migration_ids == (first.migration_id,)
        assert _ledger_ids(driver) == ()

        assert initial.apply_up() == dry_run
        assert initial.verify() == (first.migration_id,)

        upgraded = _runner(driver, (first, second), owner=f"{backend}-upgrade")
        assert upgraded.plan_up().migration_ids == (second.migration_id,)
        assert upgraded.apply_up().migration_ids == (second.migration_id,)
        assert upgraded.verify() == (first.migration_id, second.migration_id)
        assert _ledger_ids(driver) == (first.migration_id, second.migration_id)
        assert _values(driver) == ("base",)

        drifted = Migration(
            first.migration_id,
            (_statement(f"CREATE TABLE {_TABLE} (changed INTEGER)"),),
            first.down,
            first.verify,
        )
        with pytest.raises(MigrationIntegrityError):
            _runner(
                driver,
                (drifted, second),
                owner=f"{backend}-drift",
            ).plan_up()

        assert upgraded.apply_down().migration_ids == (second.migration_id,)
        assert upgraded.verify() == (first.migration_id,)
        assert _values(driver) == ()
        assert upgraded.apply_down().migration_ids == (first.migration_id,)
        assert _ledger_ids(driver) == ()
        _reset(driver)


@pytest.mark.parametrize(("backend", "environment", "factory"), _CASES)
def test_interrupted_migration_rolls_back_data_and_ledger_on_real_services(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
) -> None:
    with factory(_url(environment)) as driver:
        _reset(driver)
        first = _create_table()
        second = _seed_table()
        broken = _broken_change()
        runner = _runner(
            driver,
            (first, second, broken),
            owner=f"{backend}-interruption",
        )
        runner.bootstrap()

        with pytest.raises(DatabaseOperationError):
            runner.apply_up()

        assert _ledger_ids(driver) == (first.migration_id, second.migration_id)
        assert _values(driver) == ("base",)
        assert runner.plan_up().migration_ids == (broken.migration_id,)
        with runner.lock():
            pass
        _reset(driver)


@pytest.mark.parametrize(("backend", "environment", "factory"), _CASES)
def test_migration_lease_excludes_concurrent_real_service_owner(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
) -> None:
    url = _url(environment)
    with factory(url) as first_driver, factory(url) as second_driver:
        _reset(first_driver)
        first = _runner(first_driver, (), owner=f"{backend}-owner-a")
        second = _runner(second_driver, (), owner=f"{backend}-owner-b")
        first.bootstrap()

        with first.lock():
            with pytest.raises(MigrationLockUnavailable):
                with second.lock():
                    pass

        with second.lock():
            pass
        _reset(first_driver)
