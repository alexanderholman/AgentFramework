"""SQLite relational driver with explicit transaction and capability semantics."""

from __future__ import annotations

import re
import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Literal

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseCursor,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    UnsupportedDatabaseFeature,
)

__all__ = ("SQLiteDriver",)

_SAVEPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_FEATURES = frozenset(
    {
        DatabaseFeature.TRANSACTIONS,
        DatabaseFeature.SAVEPOINTS,
        DatabaseFeature.STREAMING,
    }
)


def _failure(error: BaseException, *, stage: str) -> DatabaseOperationError:
    return DatabaseOperationError(stage=stage, protected_cause=error)


class _SQLiteCursor(DatabaseCursor):
    def __init__(self, transaction: _SQLiteTransaction, cursor: sqlite3.Cursor) -> None:
        self._transaction = transaction
        self._cursor = cursor
        self._closed = False
        self._columns = tuple(
            column[0] for column in (() if cursor.description is None else cursor.description)
        )

    def __iter__(self) -> _SQLiteCursor:
        return self

    def __next__(self) -> DatabaseRow:
        if self._closed or not self._transaction.active:
            raise DatabaseOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="database.cursor",
            )
        try:
            raw = self._cursor.fetchone()
        except sqlite3.Error as error:
            raise _failure(error, stage="database.cursor") from None
        if raw is None:
            self.close()
            raise StopIteration
        return DatabaseRow(self._columns, tuple(raw))

    def close(self) -> None:
        if not self._closed:
            self._cursor.close()
            self._closed = True


class _SQLiteSavepoint(AbstractContextManager[None]):
    def __init__(self, transaction: _SQLiteTransaction, name: str) -> None:
        self._transaction = transaction
        self._name = name

    def __enter__(self) -> None:
        self._transaction._control(f'SAVEPOINT "{self._name}"')

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error, traceback
        if error_type is not None:
            self._transaction._control(f'ROLLBACK TO SAVEPOINT "{self._name}"')
        self._transaction._control(f'RELEASE SAVEPOINT "{self._name}"')
        return False


class _SQLiteTransaction:
    def __init__(self, connection: sqlite3.Connection, *, read_only: bool) -> None:
        self._connection = connection
        self._read_only = read_only
        self._active = True
        self._cursors: list[_SQLiteCursor] = []

    @property
    def active(self) -> bool:
        return self._active

    def _assert_active(self) -> None:
        if not self._active:
            raise DatabaseOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="database.transaction",
            )

    def _control(self, sql: str) -> None:
        self._assert_active()
        try:
            self._connection.execute(sql)
        except sqlite3.Error as error:
            raise _failure(error, stage="database.transaction") from None

    def execute(self, statement: DatabaseStatement, /) -> int:
        self._assert_active()
        if not isinstance(statement, DatabaseStatement):
            raise TypeError("database execution requires a DatabaseStatement")
        if self._read_only and not statement.read_only:
            raise DatabaseOperationError(
                reason=PlatformReason.AUTHORIZATION_DENIED,
                stage="database.read-only",
            )
        try:
            cursor = self._connection.execute(statement.sql, dict(statement.parameters))
        except sqlite3.Error as error:
            raise _failure(error, stage="database.execute") from None
        try:
            return cursor.rowcount
        finally:
            cursor.close()

    def stream(self, statement: DatabaseStatement, /) -> DatabaseCursor:
        self._assert_active()
        if not isinstance(statement, DatabaseStatement):
            raise TypeError("database query requires a DatabaseStatement")
        if not statement.read_only:
            raise DatabaseOperationError(
                reason=PlatformReason.VALIDATION_FAILED,
                stage="database.query",
            )
        try:
            raw = self._connection.execute(statement.sql, dict(statement.parameters))
        except sqlite3.Error as error:
            raise _failure(error, stage="database.query") from None
        cursor = _SQLiteCursor(self, raw)
        self._cursors.append(cursor)
        return cursor

    def query(self, statement: DatabaseStatement, /) -> tuple[DatabaseRow, ...]:
        cursor = self.stream(statement)
        try:
            return tuple(cursor)
        finally:
            cursor.close()

    def savepoint(self, name: str, /) -> AbstractContextManager[None]:
        self._assert_active()
        if type(name) is not str or _SAVEPOINT.fullmatch(name) is None:
            raise ValueError("database savepoint name must be canonical")
        return _SQLiteSavepoint(self, name)

    def finish(self) -> None:
        for cursor in self._cursors:
            cursor.close()
        self._cursors.clear()
        self._active = False


class _SQLiteTransactionContext(AbstractContextManager[_SQLiteTransaction]):
    def __init__(self, driver: SQLiteDriver, *, read_only: bool) -> None:
        self._driver = driver
        self._read_only = read_only
        self._transaction: _SQLiteTransaction | None = None

    def __enter__(self) -> _SQLiteTransaction:
        self._driver._lock.acquire()
        try:
            self._driver._assert_open()
            if self._driver._transaction_active:
                raise DatabaseOperationError(
                    reason=PlatformReason.TRANSIENT_FAILURE,
                    stage="database.transaction",
                )
            if self._read_only:
                self._driver._connection.execute("PRAGMA query_only = ON")
            self._driver._connection.execute("BEGIN")
            self._driver._transaction_active = True
            self._transaction = _SQLiteTransaction(
                self._driver._connection,
                read_only=self._read_only,
            )
            return self._transaction
        except BaseException:
            if self._read_only:
                self._driver._connection.execute("PRAGMA query_only = OFF")
            self._driver._lock.release()
            raise

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error, traceback
        transaction = self._transaction
        try:
            try:
                if error_type is None:
                    self._driver._connection.commit()
                else:
                    self._driver._connection.rollback()
            except sqlite3.Error as database_error:
                raise _failure(database_error, stage="database.transaction") from None
            finally:
                if transaction is not None:
                    transaction.finish()
                self._driver._transaction_active = False
                if self._read_only:
                    self._driver._connection.execute("PRAGMA query_only = OFF")
        finally:
            self._driver._lock.release()
        return False


class SQLiteDriver(AbstractContextManager["SQLiteDriver"]):
    """One SQLite service with serialized explicit transactions."""

    def __init__(self, database: str | Path) -> None:
        if isinstance(database, Path):
            path = str(database)
        elif type(database) is str and database:
            path = database
        else:
            raise ValueError("SQLite database path must be non-empty")
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                path,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error as error:
            raise _failure(error, stage="database.connect") from None
        self._lock = RLock()
        self._transaction_active = False
        self._closed = False

    @property
    def features(self) -> frozenset[DatabaseFeature]:
        return _FEATURES

    def supports(self, feature: DatabaseFeature, /) -> bool:
        if not isinstance(feature, DatabaseFeature):
            raise TypeError("database feature is invalid")
        return feature in _FEATURES

    def require(self, feature: DatabaseFeature, /) -> None:
        if not self.supports(feature):
            raise UnsupportedDatabaseFeature(feature)

    def _assert_open(self) -> None:
        if self._closed:
            raise DatabaseOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="database.connection",
            )

    def transaction(
        self,
        *,
        read_only: bool = False,
    ) -> AbstractContextManager[_SQLiteTransaction]:
        self._assert_open()
        if type(read_only) is not bool:
            raise ValueError("database transaction read-only flag must be boolean")
        return _SQLiteTransactionContext(self, read_only=read_only)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._transaction_active:
                raise DatabaseOperationError(
                    reason=PlatformReason.TRANSIENT_FAILURE,
                    stage="database.shutdown",
                )
            try:
                self._connection.close()
            except sqlite3.Error as error:
                raise _failure(error, stage="database.shutdown") from None
            self._closed = True

    def __enter__(self) -> SQLiteDriver:
        self._assert_open()
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        self.close()
        return False
