from __future__ import annotations

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
from af.infrastructure import postgresql as postgresql_module
from af.infrastructure.postgresql import PostgreSQLDriver


def _sqlite_engine(_url: str, **_options: object) -> Engine:
    return create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)


def _driver(monkeypatch: pytest.MonkeyPatch) -> PostgreSQLDriver:
    monkeypatch.setattr(postgresql_module, "create_engine", _sqlite_engine)
    monkeypatch.setattr(postgresql_module, "_READ_ONLY_SQL", "SELECT 1")
    return PostgreSQLDriver("postgresql+psycopg://user:secret@localhost/database")


def test_postgresql_driver_executes_and_rolls_back_with_sanitized_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _driver(monkeypatch) as driver:
        assert driver.features == frozenset(
            {
                DatabaseFeature.TRANSACTIONS,
                DatabaseFeature.TRANSACTIONAL_DDL,
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


def test_postgresql_stream_is_bounded_by_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_postgresql_cursor_close_is_idempotent_and_sanitized() -> None:
    transaction = Mock()
    transaction.active = True
    result = Mock()
    result.keys.return_value = ()
    close_failure = SQLAlchemyError("private postgresql close detail")
    result.close.side_effect = close_failure
    cursor = postgresql_module._PostgreSQLCursor(
        cast(Any, transaction),
        cast(Any, result),
    )

    with pytest.raises(DatabaseOperationError) as captured:
        cursor.close()
    cursor.close()

    assert captured.value.stage == "database.cursor-close"
    assert captured.value.protected_cause is close_failure
    assert "private postgresql close detail" not in str(captured.value)
    assert "private postgresql close detail" not in repr(captured.value)
    assert result.close.call_count == 1


def test_postgresql_read_only_and_capabilities_fail_closed(
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


def test_postgresql_rejects_invalid_url_and_sanitizes_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="postgresql\\+psycopg"):
        PostgreSQLDriver("sqlite:///:memory:")

    def fail(_url: str, **_options: object) -> Never:
        raise SQLAlchemyError("postgresql+psycopg://user:secret@private/database")

    monkeypatch.setattr(postgresql_module, "create_engine", fail)
    with pytest.raises(DatabaseOperationError) as captured:
        PostgreSQLDriver("postgresql+psycopg://user:secret@localhost/database")
    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "secret" not in str(captured.value)
    assert "secret" not in repr(captured.value)


def test_postgresql_close_is_idempotent_and_rejects_active_or_later_work(
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
