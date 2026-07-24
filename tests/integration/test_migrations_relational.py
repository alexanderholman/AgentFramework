from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from af.framework.migrations import (
    Migration,
    MigrationDirection,
    MigrationIntegrityError,
    MigrationLockUnavailable,
    MigrationMode,
    MigrationProbe,
    MigrationRunner,
)
from af.framework.relational import (
    DatabaseDriver,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    UnsupportedDatabaseFeature,
)
from af.infrastructure.mariadb import MariaDBDriver
from af.infrastructure.mysql import MySQLDriver
from af.infrastructure.postgresql import PostgreSQLDriver

_NOW = datetime(2026, 7, 24, 9, tzinfo=UTC)
_TABLE = "af_migration_conformance"
_SCHEMA_TABLE = "af_migration_schema_rollback"
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
_NON_TRANSACTIONAL_DDL_CASES = _CASES[1:]


def _statement(sql: str) -> DatabaseStatement:
    return DatabaseStatement(sql)


def _probe(sql: str, rows: tuple[DatabaseRow, ...]) -> MigrationProbe:
    return MigrationProbe(DatabaseStatement(sql, read_only=True), rows)


def _create_schema_table() -> Migration:
    return Migration(
        "20260724090600_create_schema_conformance",
        (_statement(f"CREATE TABLE {_SCHEMA_TABLE} (id INTEGER PRIMARY KEY)"),),
        (_statement(f"DROP TABLE {_SCHEMA_TABLE}"),),
        (_probe(f"SELECT id FROM {_SCHEMA_TABLE} WHERE 1 = 0", ()),),
        mode=MigrationMode.SCHEMA,
    )


def _seed_table() -> Migration:
    return Migration(
        "20260724090000_seed_conformance",
        (_statement(f"INSERT INTO {_TABLE} (id, value) VALUES (1, 'base')"),),
        (_statement(f"DELETE FROM {_TABLE} WHERE id = 1"),),
        (
            _probe(
                f"SELECT value FROM {_TABLE} WHERE id = 1",
                (DatabaseRow(("value",), ("base",)),),
            ),
        ),
        mode=MigrationMode.DATA,
    )


def _upgrade_table() -> Migration:
    return Migration(
        "20260724090100_upgrade_conformance",
        (_statement(f"INSERT INTO {_TABLE} (id, value) VALUES (2, 'upgrade')"),),
        (_statement(f"DELETE FROM {_TABLE} WHERE id = 2"),),
        (
            _probe(
                f"SELECT value FROM {_TABLE} WHERE id = 2",
                (DatabaseRow(("value",), ("upgrade",)),),
            ),
        ),
        mode=MigrationMode.DATA,
    )


def _broken_change() -> Migration:
    return Migration(
        "20260724090200_broken_conformance",
        (
            _statement(f"INSERT INTO {_TABLE} (id, value) VALUES (3, 'partial')"),
            _statement("THIS IS NOT VALID SQL"),
        ),
        mode=MigrationMode.DATA,
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


@contextmanager
def _managed_driver(
    factory: Callable[[str], DatabaseDriver],
    url: str,
) -> Iterator[DatabaseDriver]:
    driver = factory(url)
    try:
        yield driver
    finally:
        driver.close()


def _reset(driver: DatabaseDriver) -> None:
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement(f"DROP TABLE IF EXISTS {_SCHEMA_TABLE}"))
        transaction.execute(DatabaseStatement(f"DROP TABLE IF EXISTS {_TABLE}"))
        transaction.execute(DatabaseStatement("DROP TABLE IF EXISTS af_migrations"))
        transaction.execute(DatabaseStatement("DROP TABLE IF EXISTS af_migration_locks"))


def _provision_table(driver: DatabaseDriver, backend: str) -> None:
    storage = "" if backend == "postgresql" else " ENGINE=InnoDB"
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                f"""CREATE TABLE {_TABLE} (
                    id INTEGER PRIMARY KEY,
                    value VARCHAR(64) NOT NULL
                ){storage}"""
            )
        )


def _record_applied(
    driver: DatabaseDriver,
    migrations: tuple[Migration, ...],
) -> None:
    with driver.transaction() as transaction:
        for applied_order, migration in enumerate(migrations, start=1):
            transaction.execute(
                DatabaseStatement(
                    """INSERT INTO af_migrations (
                        migration_id,
                        checksum,
                        batch,
                        applied_order,
                        applied_at
                    ) VALUES (
                        :migration_id,
                        :checksum,
                        1,
                        :applied_order,
                        :applied_at
                    )""",
                    {
                        "migration_id": migration.migration_id,
                        "checksum": migration.checksum,
                        "applied_order": applied_order,
                        "applied_at": "2026-07-24T09:00:00.000000Z",
                    },
                )
            )


def _ledger_ids(driver: DatabaseDriver) -> tuple[object, ...]:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                "SELECT migration_id FROM af_migrations ORDER BY applied_order",
                read_only=True,
            )
        )
    return tuple(row["migration_id"] for row in rows)


def _ledger_checksums(driver: DatabaseDriver) -> tuple[object, ...]:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                "SELECT checksum FROM af_migrations ORDER BY applied_order",
                read_only=True,
            )
        )
    return tuple(row["checksum"] for row in rows)


def _values(driver: DatabaseDriver) -> tuple[object, ...]:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                f"SELECT value FROM {_TABLE} ORDER BY id",
                read_only=True,
            )
        )
    return tuple(row["value"] for row in rows)


def _table_exists(driver: DatabaseDriver, backend: str, table: str) -> bool:
    current_schema = "current_schema()" if backend == "postgresql" else "DATABASE()"
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                f"""SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = {current_schema}
                  AND table_name = :table_name""",
                {"table_name": table},
                read_only=True,
            )
        )
    return bool(rows)


def _assert_mysql_family_transactional_tables(driver: DatabaseDriver) -> None:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                """SELECT table_name, engine
                FROM information_schema.tables
                WHERE table_schema = DATABASE()
                  AND table_name IN (:application, :ledger, :locks)""",
                {
                    "application": _TABLE,
                    "ledger": "af_migrations",
                    "locks": "af_migration_locks",
                },
                read_only=True,
            )
        )
    assert {str(row["table_name"]): str(row["engine"]).lower() for row in rows} == {
        _TABLE: "innodb",
        "af_migrations": "innodb",
        "af_migration_locks": "innodb",
    }


@pytest.mark.parametrize(("backend", "environment", "factory"), _CASES)
def test_data_migration_lifecycle_passes_real_relational_services(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
) -> None:
    with _managed_driver(factory, _url(environment)) as driver:
        _reset(driver)
        first = _seed_table()
        second = _upgrade_table()
        initial = _runner(driver, (first,), owner=f"{backend}-initial")
        initial.bootstrap()
        initial.bootstrap()
        _provision_table(driver, backend)
        if backend != "postgresql":
            _assert_mysql_family_transactional_tables(driver)

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
        assert _values(driver) == ("base", "upgrade")

        drifted = Migration(
            first.migration_id,
            (_statement(f"INSERT INTO {_TABLE} (id, value) VALUES (3, 'drift')"),),
            first.down,
            first.verify,
            mode=MigrationMode.DATA,
        )
        with pytest.raises(MigrationIntegrityError):
            _runner(
                driver,
                (drifted, second),
                owner=f"{backend}-drift",
            ).plan_up()

        dry_down = upgraded.apply_down(dry_run=True)
        assert dry_down.migration_ids == (second.migration_id,)
        assert _ledger_ids(driver) == (first.migration_id, second.migration_id)
        assert _values(driver) == ("base", "upgrade")

        assert upgraded.apply_down() == dry_down
        assert upgraded.verify() == (first.migration_id,)
        assert _values(driver) == ("base",)
        final_dry_down = upgraded.apply_down(dry_run=True)
        assert final_dry_down.migration_ids == (first.migration_id,)
        assert _ledger_ids(driver) == (first.migration_id,)
        assert _values(driver) == ("base",)

        assert upgraded.apply_down() == final_dry_down
        assert _ledger_ids(driver) == ()
        assert _values(driver) == ()
        _reset(driver)


@pytest.mark.parametrize(("backend", "environment", "factory"), _CASES)
def test_interrupted_migration_rolls_back_data_and_ledger_on_real_services(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
) -> None:
    with _managed_driver(factory, _url(environment)) as driver:
        _reset(driver)
        first = _seed_table()
        second = _upgrade_table()
        broken = _broken_change()
        runner = _runner(
            driver,
            (first, second, broken),
            owner=f"{backend}-interruption",
        )
        runner.bootstrap()
        _provision_table(driver, backend)

        with pytest.raises(DatabaseOperationError):
            runner.apply_up()

        assert _ledger_ids(driver) == (first.migration_id, second.migration_id)
        assert _values(driver) == ("base", "upgrade")
        assert runner.plan_up().migration_ids == (broken.migration_id,)
        with runner.lock():
            pass
        _reset(driver)


def test_postgresql_schema_interruption_rolls_back_ddl_and_ledger() -> None:
    with PostgreSQLDriver(_url("AF_POSTGRESQL_TEST_URL")) as driver:
        _reset(driver)
        broken = Migration(
            "20260724090400_broken_schema_conformance",
            (
                _statement(f"CREATE TABLE {_SCHEMA_TABLE} (id INTEGER PRIMARY KEY)"),
                _statement("THIS IS NOT VALID SQL"),
            ),
            mode=MigrationMode.SCHEMA,
        )
        runner = _runner(driver, (broken,), owner="postgresql-schema-interruption")
        runner.bootstrap()

        with pytest.raises(DatabaseOperationError):
            runner.apply_up()

        assert _ledger_ids(driver) == ()
        assert not _table_exists(driver, "postgresql", _SCHEMA_TABLE)
        assert runner.plan_up().migration_ids == (broken.migration_id,)
        _reset(driver)


def test_postgresql_schema_lifecycle_creates_verifies_and_reverses() -> None:
    with PostgreSQLDriver(_url("AF_POSTGRESQL_TEST_URL")) as driver:
        _reset(driver)
        migration = _create_schema_table()
        runner = _runner(driver, (migration,), owner="postgresql-schema-lifecycle")
        runner.bootstrap()

        dry_up = runner.apply_up(dry_run=True)
        assert dry_up.migration_ids == (migration.migration_id,)
        assert not _table_exists(driver, "postgresql", _SCHEMA_TABLE)
        assert _ledger_ids(driver) == ()

        assert runner.apply_up() == dry_up
        assert _table_exists(driver, "postgresql", _SCHEMA_TABLE)
        assert _ledger_ids(driver) == (migration.migration_id,)
        assert runner.verify() == (migration.migration_id,)

        dry_down = runner.apply_down(dry_run=True)
        assert dry_down.migration_ids == (migration.migration_id,)
        assert _table_exists(driver, "postgresql", _SCHEMA_TABLE)
        assert _ledger_ids(driver) == (migration.migration_id,)

        assert runner.apply_down() == dry_down
        assert not _table_exists(driver, "postgresql", _SCHEMA_TABLE)
        assert _ledger_ids(driver) == ()
        _reset(driver)


@pytest.mark.parametrize(
    ("backend", "environment", "factory"),
    _NON_TRANSACTIONAL_DDL_CASES,
)
def test_nontransactional_ddl_service_rejects_mixed_plan_before_data_executes(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
) -> None:
    with _managed_driver(factory, _url(environment)) as driver:
        _reset(driver)
        sentinel = Migration(
            "20260724090500_data_sentinel",
            (_statement(f"INSERT INTO {_TABLE} (id, value) VALUES (9, 'sentinel')"),),
            mode=MigrationMode.DATA,
        )
        schema = _create_schema_table()
        runner = _runner(
            driver,
            (sentinel, schema),
            owner=f"{backend}-mixed-mode-preflight",
        )
        runner.bootstrap()
        _provision_table(driver, backend)
        assert runner.plan_up().migration_ids == (
            sentinel.migration_id,
            schema.migration_id,
        )

        with pytest.raises(UnsupportedDatabaseFeature) as captured:
            runner.apply_up()

        assert captured.value.feature is DatabaseFeature.TRANSACTIONAL_DDL
        assert _ledger_ids(driver) == ()
        assert _values(driver) == ()
        assert not _table_exists(driver, backend, _SCHEMA_TABLE)
        _reset(driver)


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(
    ("backend", "environment", "factory"),
    _NON_TRANSACTIONAL_DDL_CASES,
)
def test_nontransactional_ddl_service_rejects_down_before_data_executes(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
    dry_run: bool,
) -> None:
    with _managed_driver(factory, _url(environment)) as driver:
        _reset(driver)
        schema = Migration(
            "20260724090300_create_down_schema",
            (_statement(f"CREATE TABLE {_SCHEMA_TABLE} (id INTEGER PRIMARY KEY) ENGINE=InnoDB"),),
            (_statement(f"DROP TABLE {_SCHEMA_TABLE}"),),
            mode=MigrationMode.SCHEMA,
        )
        data = Migration(
            "20260724090400_seed_down_sentinel",
            (_statement(f"INSERT INTO {_TABLE} (id, value) VALUES (9, 'sentinel')"),),
            (_statement(f"DELETE FROM {_TABLE} WHERE id = 9"),),
            mode=MigrationMode.DATA,
        )
        runner = _runner(
            driver,
            (schema, data),
            owner=f"{backend}-down-mode-preflight",
        )
        runner.bootstrap()
        _provision_table(driver, backend)
        with driver.transaction() as transaction:
            transaction.execute(schema.up[0])
            transaction.execute(data.up[0])
        _record_applied(driver, (schema, data))
        assert runner.plan_down(steps=2).migration_ids == (
            data.migration_id,
            schema.migration_id,
        )

        with pytest.raises(UnsupportedDatabaseFeature) as captured:
            runner.apply_down(steps=2, dry_run=dry_run)

        assert captured.value.feature is DatabaseFeature.TRANSACTIONAL_DDL
        assert _ledger_ids(driver) == (schema.migration_id, data.migration_id)
        assert _values(driver) == ("sentinel",)
        assert _table_exists(driver, backend, _SCHEMA_TABLE)
        _reset(driver)


@pytest.mark.parametrize(
    ("backend", "environment", "factory"),
    _NON_TRANSACTIONAL_DDL_CASES,
)
def test_nontransactional_ddl_service_adopts_exact_pre_mode_data_checksum(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
) -> None:
    with _managed_driver(factory, _url(environment)) as driver:
        _reset(driver)
        current = _seed_table()
        legacy = Migration(
            current.migration_id,
            current.up,
            current.down,
            current.verify,
        )
        compatible = Migration(
            current.migration_id,
            current.up,
            current.down,
            current.verify,
            mode=MigrationMode.DATA,
            accept_pre_mode_checksum=True,
        )
        strict = _runner(
            driver,
            (current,),
            owner=f"{backend}-strict-pre-mode",
        )
        adopted = _runner(
            driver,
            (compatible,),
            owner=f"{backend}-adopted-pre-mode",
        )
        strict.bootstrap()
        _provision_table(driver, backend)
        with driver.transaction() as transaction:
            transaction.execute(current.up[0])
        _record_applied(driver, (legacy,))

        with pytest.raises(MigrationIntegrityError):
            strict.plan_up()

        assert adopted.verify() == (compatible.migration_id,)
        dry_down = adopted.apply_down(dry_run=True)
        assert dry_down.migration_ids == (compatible.migration_id,)
        assert _ledger_checksums(driver) == (legacy.checksum,)
        assert _values(driver) == ("base",)

        assert adopted.apply_down() == dry_down
        assert _ledger_ids(driver) == ()
        assert _values(driver) == ()

        assert adopted.apply_up().migration_ids == (compatible.migration_id,)
        assert _ledger_checksums(driver) == (compatible.checksum,)
        assert compatible.checksum != legacy.checksum
        assert _values(driver) == ("base",)
        _reset(driver)


@pytest.mark.parametrize(("backend", "environment", "factory"), _CASES)
def test_migration_lease_excludes_concurrent_real_service_owner(
    backend: str,
    environment: str,
    factory: Callable[[str], DatabaseDriver],
) -> None:
    url = _url(environment)
    with (
        _managed_driver(factory, url) as first_driver,
        _managed_driver(factory, url) as second_driver,
    ):
        _reset(first_driver)
        first = _runner(first_driver, (), owner=f"{backend}-owner-a")
        second = _runner(second_driver, (), owner=f"{backend}-owner-b")
        first.bootstrap()

        with (
            first.lock(),
            pytest.raises(MigrationLockUnavailable),
            second.lock(),
        ):
            pass

        with second.lock():
            pass
        _reset(first_driver)
