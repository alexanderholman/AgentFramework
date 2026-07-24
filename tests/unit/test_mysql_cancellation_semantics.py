from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from af.framework.errors import PlatformReason
from af.framework.relational import DatabaseOperationError, DatabaseStatement
from af.infrastructure import mysql as mysql_module
from af.infrastructure.mysql import MySQLDriver


class _KillerResult:
    def close(self) -> None:
        pass


class _KillerConnection:
    def __enter__(self) -> _KillerConnection:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def exec_driver_sql(self, sql: str) -> _KillerResult:
        assert sql == "KILL QUERY 7"
        return _KillerResult()


class _KillerEngine:
    def connect(self) -> _KillerConnection:
        return _KillerConnection()

    def dispose(self) -> None:
        pass


def _sqlite_engine(_url: str, **_options: object):
    return create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)


def test_caught_mysql_cancellation_taints_transaction_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mysql_module, "create_engine", _sqlite_engine)
    monkeypatch.setattr(mysql_module, "_READ_ONLY_SQL", "SELECT 1")
    driver = MySQLDriver("mysql+pymysql://user:secret@localhost/database")
    driver._thread_id = 7

    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE records (id INTEGER)"))

    monkeypatch.setattr(
        mysql_module,
        "create_engine",
        lambda *_args, **_options: _KillerEngine(),
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
