from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    UnsupportedDatabaseFeature,
)
from af.infrastructure import sqlite as sqlite_module
from af.infrastructure.sqlite import SQLiteDriver
from af.testing.conformance import (
    ConformanceCase,
    ConformanceEvidence,
    DriverConformanceSuite,
    RealServiceHook,
)


def _create_schema(driver: SQLiteDriver) -> None:
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                "CREATE TABLE records (id INTEGER PRIMARY KEY, tenant TEXT, value TEXT)"
            )
        )


def _assert_commit(driver: SQLiteDriver) -> None:
    _create_schema(driver)
    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                "INSERT INTO records (tenant, value) VALUES (:tenant, :value)",
                {"tenant": "tenant-a", "value": "committed"},
            )
        )
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                "SELECT tenant, value FROM records ORDER BY id",
                read_only=True,
            )
        )
    assert [row.as_mapping() for row in rows] == [{"tenant": "tenant-a", "value": "committed"}]


def _assert_rollback(driver: SQLiteDriver) -> None:
    _create_schema(driver)
    with pytest.raises(RuntimeError, match="rollback"), driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                "INSERT INTO records (tenant, value) VALUES (:tenant, :value)",
                {"tenant": "tenant-a", "value": "discarded"},
            )
        )
        raise RuntimeError("rollback")
    with driver.transaction(read_only=True) as transaction:
        assert transaction.query(DatabaseStatement("SELECT id FROM records", read_only=True)) == ()


def test_sqlite_passes_shared_real_driver_transaction_suite() -> None:
    @contextmanager
    def fixture():  # type: ignore[no-untyped-def]
        with SQLiteDriver(":memory:") as driver:
            yield driver

    suite = DriverConformanceSuite(
        "relational-driver",
        (
            ConformanceCase("commit", _assert_commit),
            ConformanceCase("rollback", _assert_rollback),
        ),
    )

    report = suite.run_real(
        RealServiceHook("sqlite", fixture, enabled=True),
    )

    assert report.evidence is ConformanceEvidence.REAL_SERVICE
    assert report.integration == "sqlite"
    assert report.cases == ("commit", "rollback")


def test_sqlite_savepoint_rolls_back_only_nested_work() -> None:
    with SQLiteDriver(":memory:") as driver:
        _create_schema(driver)
        with driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement("INSERT INTO records (tenant, value) VALUES ('tenant-a', 'kept')")
            )
            with pytest.raises(ValueError, match="nested"), transaction.savepoint("nested_attempt"):
                transaction.execute(
                    DatabaseStatement(
                        "INSERT INTO records (tenant, value) VALUES ('tenant-a', 'discarded')"
                    )
                )
                raise ValueError("nested")
        with driver.transaction(read_only=True) as transaction:
            rows = transaction.query(
                DatabaseStatement(
                    "SELECT value FROM records ORDER BY id",
                    read_only=True,
                )
            )

    assert [row["value"] for row in rows] == ["kept"]


def test_sqlite_stream_is_bounded_by_transaction_lifetime() -> None:
    with SQLiteDriver(":memory:") as driver:
        _create_schema(driver)
        with driver.transaction() as transaction:
            for value in ("first", "second"):
                transaction.execute(
                    DatabaseStatement(
                        "INSERT INTO records (tenant, value) VALUES ('tenant-a', :value)",
                        {"value": value},
                    )
                )
        with driver.transaction(read_only=True) as transaction:
            cursor = transaction.stream(
                DatabaseStatement(
                    "SELECT value FROM records ORDER BY id",
                    read_only=True,
                )
            )
            assert next(cursor)["value"] == "first"

        with pytest.raises(DatabaseOperationError):
            next(cursor)


def test_sqlite_cursor_close_is_idempotent_and_sanitized() -> None:
    transaction = Mock()
    transaction.active = True
    raw_cursor = Mock()
    raw_cursor.description = ()
    close_failure = sqlite3.OperationalError("private sqlite close detail")
    raw_cursor.close.side_effect = close_failure
    cursor = sqlite_module._SQLiteCursor(
        cast(Any, transaction),
        cast(Any, raw_cursor),
    )

    with pytest.raises(DatabaseOperationError) as captured:
        cursor.close()
    cursor.close()

    assert captured.value.stage == "database.cursor-close"
    assert captured.value.protected_cause is close_failure
    assert "private sqlite close detail" not in str(captured.value)
    assert "private sqlite close detail" not in repr(captured.value)
    assert raw_cursor.close.call_count == 1


def test_sqlite_read_only_mode_cannot_be_bypassed_by_statement_metadata() -> None:
    with SQLiteDriver(":memory:") as driver:
        _create_schema(driver)
        with driver.transaction(read_only=True) as transaction:
            with pytest.raises(DatabaseOperationError) as captured:
                transaction.execute(
                    DatabaseStatement(
                        "INSERT INTO records (tenant, value) VALUES ('tenant-a', 'denied')"
                    )
                )
            assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED

            with pytest.raises(DatabaseOperationError):
                transaction.query(
                    DatabaseStatement(
                        "DELETE FROM records",
                        read_only=True,
                    )
                )


def test_sqlite_file_persists_across_driver_restart(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "database.sqlite3"
    with SQLiteDriver(path) as driver:
        _create_schema(driver)
        with driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    "INSERT INTO records (tenant, value) VALUES ('tenant-a', 'persisted')"
                )
            )

    with SQLiteDriver(path) as driver, driver.transaction(read_only=True) as transaction:
        rows = transaction.query(DatabaseStatement("SELECT value FROM records", read_only=True))

    assert rows[0]["value"] == "persisted"


def test_sqlite_capabilities_fail_explicitly_and_close_is_idempotent() -> None:
    driver = SQLiteDriver(":memory:")

    assert driver.supports(DatabaseFeature.TRANSACTIONS)
    assert driver.supports(DatabaseFeature.TRANSACTIONAL_DDL)
    assert driver.supports(DatabaseFeature.SAVEPOINTS)
    assert driver.supports(DatabaseFeature.STREAMING)
    assert driver.supports(DatabaseFeature.CANCELLATION)
    assert not driver.supports(DatabaseFeature.ADVISORY_LOCKS)
    with pytest.raises(UnsupportedDatabaseFeature) as captured:
        driver.require(DatabaseFeature.ADVISORY_LOCKS)
    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE

    driver.close()
    driver.close()
    with pytest.raises(DatabaseOperationError) as closed:
        driver.transaction()
    assert closed.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_sqlite_rejects_nested_top_level_transactions_and_active_shutdown() -> None:
    driver = SQLiteDriver(":memory:")
    with driver.transaction():
        with pytest.raises(DatabaseOperationError) as captured, driver.transaction():
            pass
        assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
        with pytest.raises(DatabaseOperationError) as captured:
            driver.close()
        assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    driver.close()


def test_statement_validation_and_database_failures_do_not_disclose_sql() -> None:
    with pytest.raises(ValueError):
        DatabaseStatement("")
    with pytest.raises(ValueError):
        DatabaseStatement("SELECT 1", {"unsafe": object()})

    secret_sql = "SELECT secret_password FROM missing_private_table"
    with (
        SQLiteDriver(":memory:") as driver,
        pytest.raises(DatabaseOperationError) as captured,
        driver.transaction(read_only=True) as transaction,
    ):
        transaction.query(DatabaseStatement(secret_sql, read_only=True))

    assert secret_sql not in str(captured.value)
    assert "secret_password" not in repr(captured.value)
    assert captured.value.protected_cause is not None


def test_statement_snapshots_supported_values_and_redacts_representation() -> None:
    parameters = {
        "nothing": None,
        "flag": True,
        "count": 3,
        "ratio": 0.5,
        "text": "safe",
        "binary": b"safe",
        "day": date(2026, 7, 24),
        "instant": datetime(2026, 7, 24, tzinfo=UTC),
    }

    statement = DatabaseStatement("SELECT :text", parameters, read_only=True)
    parameters["text"] = "changed"

    assert statement.parameters["text"] == "safe"
    assert "SELECT" not in repr(statement)
    assert "safe" not in repr(statement)
    with pytest.raises(TypeError):
        statement.parameters["text"] = "mutate"  # type: ignore[index]


@pytest.mark.parametrize(
    ("sql", "parameters", "read_only"),
    [
        ("SELECT \x00", None, False),
        ("SELECT 1", [], False),
        ("SELECT 1", {"not valid": 1}, False),
        ("SELECT 1", {"infinite": float("inf")}, False),
        ("SELECT 1", None, 1),
    ],
)
def test_statement_rejects_noncanonical_inputs(sql, parameters, read_only) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        DatabaseStatement(sql, parameters, read_only=read_only)


def test_database_row_validates_shape_lookup_and_redacted_repr() -> None:
    row = DatabaseRow(("name", "count"), ("private", 3))

    assert row["count"] == 3
    assert row.as_mapping() == {"name": "private", "count": 3}
    assert "private" not in repr(row)
    with pytest.raises(KeyError):
        row["missing"]
    with pytest.raises(TypeError):
        row[1]  # type: ignore[index]
    with pytest.raises(ValueError):
        DatabaseRow(("duplicate", "duplicate"), (1, 2))
    with pytest.raises(ValueError):
        DatabaseRow(("one",), (1, 2))


def test_sqlite_rejects_invalid_contract_calls_and_finished_transaction_use() -> None:
    with SQLiteDriver(":memory:") as driver:
        assert driver.features == frozenset(
            {
                DatabaseFeature.TRANSACTIONS,
                DatabaseFeature.TRANSACTIONAL_DDL,
                DatabaseFeature.SAVEPOINTS,
                DatabaseFeature.STREAMING,
                DatabaseFeature.CANCELLATION,
            }
        )
        with pytest.raises(TypeError):
            driver.supports("transactions")  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            driver.transaction(read_only=1)  # type: ignore[arg-type]

        with driver.transaction() as transaction:
            with pytest.raises(TypeError):
                transaction.execute(object())  # type: ignore[arg-type]
            with pytest.raises(TypeError):
                transaction.stream(object())  # type: ignore[arg-type]
            with pytest.raises(DatabaseOperationError):
                transaction.query(DatabaseStatement("SELECT 1"))
            with pytest.raises(ValueError):
                transaction.savepoint("not valid")

        with pytest.raises(DatabaseOperationError):
            transaction.execute(DatabaseStatement("SELECT 1"))


def test_exhausted_cursor_closes_itself() -> None:
    with SQLiteDriver(":memory:") as driver, driver.transaction(read_only=True) as transaction:
        cursor = transaction.stream(DatabaseStatement("SELECT 1 AS value", read_only=True))
        assert next(cursor)["value"] == 1
        with pytest.raises(StopIteration):
            next(cursor)
        with pytest.raises(DatabaseOperationError):
            next(cursor)
