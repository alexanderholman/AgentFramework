from __future__ import annotations

from threading import Event
from typing import Any, Never, cast
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseStatement,
    UnsupportedDatabaseFeature,
)
from af.infrastructure import mysql as mysql_module
from af.infrastructure.mysql import MySQLDriver


def _sqlite_engine(_url: str, **_options: object) -> Engine:
    return create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)


def _driver(monkeypatch: pytest.MonkeyPatch) -> MySQLDriver:
    monkeypatch.setattr(mysql_module, "create_engine", _sqlite_engine)
    monkeypatch.setattr(mysql_module, "_READ_ONLY_SQL", "SELECT 1")
    return MySQLDriver("mysql+pymysql://user:secret@localhost/database")


def test_mysql_driver_executes_and_rolls_back_with_sanitized_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _driver(monkeypatch) as driver:
        assert driver.features == frozenset(
            {
                DatabaseFeature.TRANSACTIONS,
                DatabaseFeature.SAVEPOINTS,
                DatabaseFeature.STREAMING,
            }
        )
        with driver.transaction() as transaction:
            transaction.execute(DatabaseStatement("CREATE TABLE records (id INTEGER, value TEXT)"))
            transaction.execute(
                DatabaseStatement(
                    "INSERT INTO records (id, value) VALUES (:id, :value)",
                    {"id": 1, "value": "kept"},
                )
            )
            with pytest.raises(RuntimeError, match="discard"), transaction.savepoint("nested"):
                transaction.execute(
                    DatabaseStatement(
                        "INSERT INTO records (id, value) VALUES (:id, :value)",
                        {"id": 2, "value": "discarded"},
                    )
                )
                raise RuntimeError("discard")
        with driver.transaction(read_only=True) as transaction:
            rows = transaction.query(
                DatabaseStatement(
                    "SELECT id, value FROM records ORDER BY id",
                    read_only=True,
                )
            )
        assert [row.as_mapping() for row in rows] == [{"id": 1, "value": "kept"}]


def test_mysql_stream_is_bounded_by_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    with _driver(monkeypatch) as driver:
        with driver.transaction() as transaction:
            transaction.execute(DatabaseStatement("CREATE TABLE records (id INTEGER)"))
            transaction.execute(DatabaseStatement("INSERT INTO records VALUES (1)"))
        with driver.transaction(read_only=True) as transaction:
            cursor = transaction.stream(DatabaseStatement("SELECT id FROM records", read_only=True))
            assert next(cursor)["id"] == 1
        with pytest.raises(DatabaseOperationError) as captured:
            next(cursor)
        assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_mysql_cursor_close_is_idempotent_and_sanitized() -> None:
    transaction = Mock()
    transaction.active = True
    result = Mock()
    result.keys.return_value = ()
    close_failure = SQLAlchemyError("private mysql close detail")
    result.close.side_effect = close_failure
    cursor = mysql_module._MySQLCursor(
        cast(Any, transaction),
        cast(Any, result),
    )

    with pytest.raises(DatabaseOperationError) as captured:
        cursor.close()
    cursor.close()

    assert captured.value.stage == "database.cursor-close"
    assert captured.value.protected_cause is close_failure
    assert "private mysql close detail" not in str(captured.value)
    assert "private mysql close detail" not in repr(captured.value)
    assert result.close.call_count == 1


@pytest.mark.parametrize("cancellation_states", [(True,), (False, True)])
def test_mysql_cursor_cancellation_precedes_close_failure(
    cancellation_states: tuple[bool, ...],
) -> None:
    transaction = Mock()
    transaction.active = True
    transaction.cancellation_requested.side_effect = cancellation_states
    result = Mock()
    result.keys.return_value = ("value",)
    result.fetchone.return_value = (1,)
    close_failure = SQLAlchemyError("private cancelled cursor close detail")
    result.close.side_effect = close_failure
    cursor = mysql_module._MySQLCursor(
        cast(Any, transaction),
        cast(Any, result),
    )

    with pytest.raises(DatabaseOperationError) as captured:
        next(cursor)

    assert captured.value.reason is PlatformReason.CANCELLED
    assert captured.value.stage == "database.cursor"
    cleanup_failure = captured.value.protected_cause
    assert isinstance(cleanup_failure, DatabaseOperationError)
    assert cleanup_failure.protected_cause is close_failure
    assert "private cancelled cursor close detail" not in str(captured.value)
    assert "private cancelled cursor close detail" not in repr(captured.value)
    assert result.close.call_count == 1


@pytest.mark.parametrize(
    ("operation", "statement", "stage"),
    [
        ("execute", DatabaseStatement("SELECT 1"), "database.execute"),
        (
            "stream",
            DatabaseStatement("SELECT 1", read_only=True),
            "database.query",
        ),
    ],
)
def test_mysql_post_execution_cancellation_precedes_result_close_failure(
    operation: str,
    statement: DatabaseStatement,
    stage: str,
) -> None:
    cancellation = Event()
    result = Mock()
    result.keys.return_value = ()
    close_failure = SQLAlchemyError("private cancelled result close detail")
    result.close.side_effect = close_failure
    connection = Mock()

    def execute(*_args: object, **_kwargs: object) -> Mock:
        cancellation.set()
        return result

    connection.execute.side_effect = execute
    transaction = mysql_module._MySQLTransaction(
        cast(Any, connection),
        cancellation,
        read_only=False,
    )

    with pytest.raises(DatabaseOperationError) as captured:
        getattr(transaction, operation)(statement)

    assert captured.value.reason is PlatformReason.CANCELLED
    assert captured.value.stage == stage
    assert captured.value.protected_cause is close_failure
    assert "private cancelled result close detail" not in str(captured.value)
    assert "private cancelled result close detail" not in repr(captured.value)
    assert result.close.call_count == 1


def test_mysql_read_only_and_capabilities_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _driver(monkeypatch) as driver:
        with (
            pytest.raises(DatabaseOperationError) as captured,
            driver.transaction(read_only=True) as transaction,
        ):
            transaction.execute(DatabaseStatement("CREATE TABLE denied (id INTEGER)"))
        assert captured.value.reason is PlatformReason.AUTHORIZATION_DENIED
        with pytest.raises(UnsupportedDatabaseFeature):
            driver.require(DatabaseFeature.JSON)
        with pytest.raises(UnsupportedDatabaseFeature):
            driver.cancel()


def test_mysql_rejects_invalid_url_and_sanitizes_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="mysql\\+pymysql"):
        MySQLDriver("sqlite:///:memory:")

    def fail(_url: str, **_options: object) -> Never:
        raise SQLAlchemyError("mysql+pymysql://user:secret@private/database")

    monkeypatch.setattr(mysql_module, "create_engine", fail)
    with pytest.raises(DatabaseOperationError) as captured:
        MySQLDriver("mysql+pymysql://user:secret@localhost/database")
    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "secret" not in str(captured.value)
    assert "secret" not in repr(captured.value)


def test_mysql_close_is_idempotent_and_rejects_active_or_later_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver(monkeypatch)
    context = driver.transaction()
    context.__enter__()
    with pytest.raises(DatabaseOperationError) as captured:
        driver.close()
    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    context.__exit__(None, None, None)
    driver.close()
    driver.close()
    with pytest.raises(DatabaseOperationError) as closed:
        driver.transaction()
    assert closed.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_mysql_cancellation_marker_discards_results_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _driver(monkeypatch) as driver:
        with (
            pytest.raises(DatabaseOperationError) as executed,
            driver.transaction() as transaction,
        ):
            driver._cancellation.set()
            transaction.execute(DatabaseStatement("SELECT 1"))
        assert executed.value.reason is PlatformReason.CANCELLED

        with (
            pytest.raises(DatabaseOperationError) as streamed,
            driver.transaction(read_only=True) as transaction,
        ):
            cursor = transaction.stream(DatabaseStatement("SELECT 1 AS value", read_only=True))
            driver._cancellation.set()
            next(cursor)
        assert streamed.value.reason is PlatformReason.CANCELLED

        with driver.transaction(read_only=True) as transaction:
            rows = transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))
        assert rows[0]["value"] == 1


def test_mysql_cancel_uses_separate_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    class KillerResult:
        closed = False

        def close(self) -> None:
            self.closed = True

    class KillerConnection:
        def __init__(self, result: KillerResult) -> None:
            self.result = result

        def __enter__(self) -> KillerConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def exec_driver_sql(self, sql: str) -> KillerResult:
            assert sql == "KILL QUERY 123"
            return self.result

    class KillerEngine:
        def __init__(self) -> None:
            self.result = KillerResult()
            self.disposed = False

        def connect(self) -> KillerConnection:
            return KillerConnection(self.result)

        def dispose(self) -> None:
            self.disposed = True

    driver = _driver(monkeypatch)
    killer = KillerEngine()
    driver._thread_id = 123
    driver._transaction_active = True
    monkeypatch.setattr(mysql_module, "create_engine", lambda *_args, **_kwargs: killer)

    driver.cancel()

    assert driver._cancellation.is_set()
    assert killer.result.closed
    assert killer.disposed
    driver._transaction_active = False
    driver.close()
