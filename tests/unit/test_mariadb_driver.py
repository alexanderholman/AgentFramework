from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseStatement,
    UnsupportedDatabaseFeature,
)
from af.infrastructure import mariadb as mariadb_module
from af.infrastructure import mysql as mysql_module
from af.infrastructure.mariadb import MariaDBDriver


def _sqlite_engine(_url: str, **_options: object):
    return create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)


def _driver(monkeypatch: pytest.MonkeyPatch) -> MariaDBDriver:
    monkeypatch.setattr(mariadb_module, "create_engine", _sqlite_engine)
    monkeypatch.setattr(mysql_module, "_READ_ONLY_SQL", "SELECT 1")
    return MariaDBDriver("mariadb+pymysql://user:secret@localhost/database")


def test_mariadb_driver_executes_savepoints_and_read_only_transactions(
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
        assert not driver.supports(DatabaseFeature.TRANSACTIONAL_DDL)
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
            with transaction.savepoint("released"):
                transaction.execute(
                    DatabaseStatement(
                        "INSERT INTO records (id, value) VALUES (:id, :value)",
                        {"id": 3, "value": "released"},
                    )
                )
        with driver.transaction(read_only=True) as transaction:
            rows = transaction.query(
                DatabaseStatement(
                    "SELECT id, value FROM records ORDER BY id",
                    read_only=True,
                )
            )
        assert [row.as_mapping() for row in rows] == [
            {"id": 1, "value": "kept"},
            {"id": 3, "value": "released"},
        ]
        with (
            pytest.raises(DatabaseOperationError) as denied,
            driver.transaction(read_only=True) as transaction,
        ):
            transaction.execute(DatabaseStatement("CREATE TABLE denied (id INTEGER)"))
        assert denied.value.reason is PlatformReason.AUTHORIZATION_DENIED
        with pytest.raises(UnsupportedDatabaseFeature):
            driver.require(DatabaseFeature.JSON)


def test_mariadb_constructor_validation_and_connection_failure_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        MariaDBDriver("")
    with pytest.raises(ValueError, match="non-empty"):
        MariaDBDriver(cast(str, None))

    def invalid_url(_url: str):
        raise ValueError("invalid")

    monkeypatch.setattr(mariadb_module, "make_url", invalid_url)
    with pytest.raises(ValueError, match="URL is invalid"):
        MariaDBDriver("mariadb+pymysql://localhost/database")

    monkeypatch.undo()

    class FailingEngine:
        def __init__(self) -> None:
            self.disposed = False

        def connect(self):
            raise SQLAlchemyError("connect failed")

        def dispose(self) -> None:
            self.disposed = True

    engine = FailingEngine()
    monkeypatch.setattr(mariadb_module, "create_engine", lambda *_args, **_options: engine)
    with pytest.raises(DatabaseOperationError) as captured:
        MariaDBDriver("mariadb+pymysql://user:secret@localhost/database")
    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert engine.disposed


def test_mariadb_rejects_other_dialects_and_sanitizes_connect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="mariadb\\+pymysql"):
        MariaDBDriver("mysql+pymysql://localhost/database")

    def fail(_url: str, **_options: object):
        raise SQLAlchemyError("mariadb+pymysql://user:secret@private/database")

    monkeypatch.setattr(mariadb_module, "create_engine", fail)
    with pytest.raises(DatabaseOperationError) as captured:
        MariaDBDriver("mariadb+pymysql://user:secret@localhost/database")
    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "secret" not in str(captured.value)
    assert "secret" not in repr(captured.value)


def test_mariadb_transaction_contract_fails_closed_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = _driver(monkeypatch)
    with pytest.raises(ValueError, match="boolean"):
        driver.transaction(read_only=cast(bool, 1))

    with driver.transaction() as transaction:
        with (
            pytest.raises(DatabaseOperationError) as nested,
            driver.transaction(),
        ):
            pass
        assert nested.value.reason is PlatformReason.TRANSIENT_FAILURE
        with pytest.raises(ValueError, match="canonical"):
            transaction.savepoint("not-canonical!")
        with pytest.raises(TypeError, match="DatabaseStatement"):
            transaction.execute(cast(DatabaseStatement, object()))
        with pytest.raises(TypeError, match="DatabaseStatement"):
            transaction.stream(cast(DatabaseStatement, object()))
        with pytest.raises(DatabaseOperationError) as invalid_query:
            transaction.stream(DatabaseStatement("SELECT 1"))
        assert invalid_query.value.reason is PlatformReason.VALIDATION_FAILED

    driver._thread_id = 7
    driver.cancel()
    driver.close()
    driver.cancel()
    with pytest.raises(DatabaseOperationError) as closed, driver:
        pass
    assert closed.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_mariadb_stream_is_bounded_by_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    with _driver(monkeypatch) as driver:
        with driver.transaction(read_only=True) as transaction:
            cursor = transaction.stream(DatabaseStatement("SELECT 1 AS value", read_only=True))
            assert next(cursor)["value"] == 1
        with pytest.raises(DatabaseOperationError) as captured:
            next(cursor)
        assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_mariadb_cancel_uses_separate_connection(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_caught_mariadb_cancellation_taints_transaction_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class KillerResult:
        def close(self) -> None:
            pass

    class KillerConnection:
        def __enter__(self) -> KillerConnection:
            return self

        def __exit__(self, *_args: object) -> None:
            pass

        def exec_driver_sql(self, sql: str) -> KillerResult:
            assert sql == "KILL QUERY 7"
            return KillerResult()

    class KillerEngine:
        def connect(self) -> KillerConnection:
            return KillerConnection()

        def dispose(self) -> None:
            pass

    driver = _driver(monkeypatch)
    driver._thread_id = 7
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE records (id INTEGER)"))

    monkeypatch.setattr(
        mysql_module,
        "create_engine",
        lambda *_args, **_options: KillerEngine(),
    )
    with (
        pytest.raises(DatabaseOperationError) as outer,
        driver.transaction() as transaction,
    ):
        transaction.execute(DatabaseStatement("INSERT INTO records VALUES (1)"))
        driver.cancel()
        with pytest.raises(DatabaseOperationError) as inner:
            transaction.execute(DatabaseStatement("SELECT 1", read_only=True))
        assert inner.value.reason is PlatformReason.CANCELLED
    assert outer.value.reason is PlatformReason.CANCELLED

    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement("SELECT COUNT(*) AS count FROM records", read_only=True)
        )
    assert rows[0]["count"] == 0
    driver.close()
