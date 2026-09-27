"""Adapter error evidence stays typed across the projection boundary."""

from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseOperationError,
    DatabaseStatement,
    DatabaseUniqueConflict,
)
from af.infrastructure import mysql, postgresql
from af.infrastructure.sqlite import SQLiteDriver


def test_sqlite_unique_conflict_is_typed_without_reclassifying_check_failure() -> None:
    with SQLiteDriver(":memory:") as driver:
        with driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    "CREATE TABLE items (id INTEGER PRIMARY KEY, value INTEGER CHECK (value > 0))"
                )
            )
            transaction.execute(DatabaseStatement("INSERT INTO items VALUES (1, 1)"))
        with pytest.raises(DatabaseUniqueConflict), driver.transaction() as transaction:
            transaction.execute(DatabaseStatement("INSERT INTO items VALUES (1, 2)"))
        with pytest.raises(DatabaseOperationError) as captured, driver.transaction() as transaction:
            transaction.execute(DatabaseStatement("INSERT INTO items VALUES (2, -1)"))
        assert not isinstance(captured.value, DatabaseUniqueConflict)


@pytest.mark.parametrize("code,conflict", [("23505", True), ("23503", False), ("XX001", False)])
def test_postgresql_unique_code_is_translated_without_exposing_evidence(
    code: str, conflict: bool
) -> None:
    error = IntegrityError("private statement", {}, SimpleNamespace(sqlstate=code))
    result = postgresql._failure(error, stage="database.execute")
    assert isinstance(result, DatabaseUniqueConflict) is conflict
    assert "private" not in str(result)
    assert "private" not in repr(result)
    if conflict:
        assert result.reason is PlatformReason.INTEGRITY_FAILURE


@pytest.mark.parametrize("code,conflict", [(1062, True), (1452, False), (1034, False)])
def test_mysql_unique_code_is_translated_without_exposing_evidence(
    code: int, conflict: bool
) -> None:
    error = IntegrityError("private statement", {}, Exception(code, "private key"))
    result = mysql._failure(error, stage="database.execute")
    assert isinstance(result, DatabaseUniqueConflict) is conflict
    assert "private" not in str(result)
    assert "private" not in repr(result)
