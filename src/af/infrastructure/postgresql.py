"""PostgreSQL relational driver with explicit transaction and capability semantics."""

from __future__ import annotations

import re
from contextlib import AbstractContextManager
from threading import RLock
from types import TracebackType
from typing import Literal, cast

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, CursorResult, Engine, RootTransaction, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseCursor,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    DatabaseUniqueConflict,
    DatabaseValue,
    UnsupportedDatabaseFeature,
)

__all__ = ("PostgreSQLDriver",)

_SAVEPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_OPERATION_CLAIMS = 1_024
_CORE_FEATURES = frozenset(
    {
        DatabaseFeature.TRANSACTIONS,
        DatabaseFeature.TRANSACTIONAL_DDL,
        DatabaseFeature.SAVEPOINTS,
        DatabaseFeature.STREAMING,
    }
)
_READ_ONLY_SQL = "SET TRANSACTION READ ONLY"


def _cancelled(error: BaseException) -> bool:
    original = getattr(error, "orig", error)
    return getattr(original, "sqlstate", None) == "57014"


def _failure(error: BaseException, *, stage: str) -> DatabaseOperationError:
    original = getattr(error, "orig", error)
    if getattr(original, "sqlstate", None) == "23505":
        return DatabaseUniqueConflict(stage=stage, protected_cause=error)
    if _cancelled(error):
        reason = PlatformReason.CANCELLED
    elif stage == "database.connect":
        reason = PlatformReason.TRANSIENT_FAILURE
    else:
        reason = PlatformReason.PERMANENT_FAILURE
    return DatabaseOperationError(
        reason=reason,
        stage=stage,
        protected_cause=error,
    )


class _PostgreSQLCursor(DatabaseCursor):
    def __init__(
        self,
        transaction: _PostgreSQLTransaction,
        result: CursorResult[tuple[object, ...]],
    ) -> None:
        self._transaction = transaction
        self._result = result
        self._closed = False
        self._columns = tuple(map(str, result.keys()))

    def __iter__(self) -> _PostgreSQLCursor:
        return self

    def __next__(self) -> DatabaseRow:
        if self._closed or not self._transaction.active:
            raise DatabaseOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="database.cursor",
            )
        try:
            raw = self._result.fetchone()
        except SQLAlchemyError as error:
            raise _failure(error, stage="database.cursor") from None
        if raw is None:
            self.close()
            raise StopIteration
        values = cast(tuple[DatabaseValue, ...], tuple(raw))
        try:
            return DatabaseRow(self._columns, values)
        except ValueError as error:
            raise DatabaseOperationError(
                reason=PlatformReason.VALIDATION_FAILED,
                stage="database.row",
                protected_cause=error,
            ) from None

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._result.close()
            except Exception as error:
                raise _failure(error, stage="database.cursor-close") from None


class _PostgreSQLSavepoint(AbstractContextManager[None]):
    def __init__(self, transaction: _PostgreSQLTransaction, name: str) -> None:
        self._transaction = transaction
        self._name = name
        self._entered = False

    def __enter__(self) -> None:
        self._transaction._control(f'SAVEPOINT "{self._name}"')
        self._entered = True

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error, traceback
        if not self._entered:
            raise DatabaseOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="database.savepoint",
            )
        if error_type is not None:
            self._transaction._control(f'ROLLBACK TO SAVEPOINT "{self._name}"')
        self._transaction._control(f'RELEASE SAVEPOINT "{self._name}"')
        return False


class _PostgreSQLTransaction:
    def __init__(self, connection: Connection, *, read_only: bool) -> None:
        self._connection = connection
        self._read_only = read_only
        self._active = True
        self._rollback_only = False
        self._operation_claims: set[str] = set()
        self._cursors: list[_PostgreSQLCursor] = []

    @property
    def active(self) -> bool:
        return self._active

    @property
    def rollback_only(self) -> bool:
        return self._rollback_only

    def _assert_active(self) -> None:
        if not self._active:
            raise DatabaseOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="database.transaction",
            )

    def _control(self, sql: str) -> None:
        self._assert_active()
        try:
            result = self._connection.exec_driver_sql(sql)
        except SQLAlchemyError as error:
            raise _failure(error, stage="database.savepoint") from None
        result.close()

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
            result = self._connection.execute(text(statement.sql), dict(statement.parameters))
        except SQLAlchemyError as error:
            raise _failure(error, stage="database.execute") from None
        try:
            return result.rowcount
        finally:
            result.close()

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
            result = self._connection.execute(
                text(statement.sql).execution_options(stream_results=True),
                dict(statement.parameters),
            )
        except SQLAlchemyError as error:
            raise _failure(error, stage="database.query") from None
        cursor = _PostgreSQLCursor(
            self,
            cast(CursorResult[tuple[object, ...]], result),
        )
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
        return _PostgreSQLSavepoint(self, name)

    def mark_rollback_only(self) -> None:
        self._assert_active()
        self._rollback_only = True

    def claim_operation(self, name: str, /) -> bool:
        self._assert_active()
        if type(name) is not str or _SAVEPOINT.fullmatch(name) is None:
            raise ValueError("database operation claim must be canonical")
        if name in self._operation_claims:
            return False
        if len(self._operation_claims) >= _MAX_OPERATION_CLAIMS:
            raise DatabaseOperationError(
                reason=PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="database.transaction.claim",
            )
        self._operation_claims.add(name)
        return True

    def finish(self) -> None:
        for cursor in self._cursors:
            cursor.close()
        self._cursors.clear()
        self._operation_claims.clear()
        self._active = False


class _PostgreSQLTransactionContext(AbstractContextManager[_PostgreSQLTransaction]):
    def __init__(self, driver: PostgreSQLDriver, *, read_only: bool) -> None:
        self._driver = driver
        self._read_only = read_only
        self._transaction: _PostgreSQLTransaction | None = None
        self._root: RootTransaction | None = None

    def __enter__(self) -> _PostgreSQLTransaction:
        self._driver._lock.acquire()
        try:
            self._driver._assert_open()
            if self._driver._transaction_active:
                raise DatabaseOperationError(
                    reason=PlatformReason.TRANSIENT_FAILURE,
                    stage="database.transaction",
                )
            root = self._driver._connection.begin()
            try:
                if self._read_only:
                    result = self._driver._connection.exec_driver_sql(_READ_ONLY_SQL)
                    result.close()
            except SQLAlchemyError:
                root.rollback()
                raise
            self._driver._transaction_active = True
            self._root = root
            self._transaction = _PostgreSQLTransaction(
                self._driver._connection,
                read_only=self._read_only,
            )
            return self._transaction
        except SQLAlchemyError as error:
            self._driver._lock.release()
            raise _failure(error, stage="database.transaction") from None
        except BaseException:
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
        root = self._root
        rollback_only = transaction is not None and transaction.rollback_only
        try:
            if root is None:
                raise DatabaseOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="database.transaction",
                )
            try:
                if error_type is None and not rollback_only:
                    root.commit()
                else:
                    root.rollback()
            except SQLAlchemyError as database_error:
                raise _failure(database_error, stage="database.transaction") from None
            finally:
                if transaction is not None:
                    transaction.finish()
                self._driver._transaction_active = False
        finally:
            self._driver._lock.release()
        if rollback_only and error_type is None:
            raise DatabaseOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="database.transaction.rollback-only",
            )
        return False


class PostgreSQLDriver(AbstractContextManager["PostgreSQLDriver"]):
    """One PostgreSQL connection with serialized explicit transactions."""

    def __init__(self, url: str) -> None:
        if type(url) is not str or not url:
            raise ValueError("PostgreSQL URL must be non-empty")
        try:
            parsed = make_url(url)
        except (TypeError, ValueError):
            raise ValueError("PostgreSQL URL is invalid") from None
        if parsed.drivername != "postgresql+psycopg":
            raise ValueError("PostgreSQL driver requires a postgresql+psycopg URL")
        try:
            engine = create_engine(url, poolclass=StaticPool)
        except (ImportError, SQLAlchemyError) as error:
            raise _failure(error, stage="database.connect") from None
        try:
            connection = engine.connect()
        except SQLAlchemyError as error:
            engine.dispose()
            raise _failure(error, stage="database.connect") from None
        self._engine: Engine = engine
        self._connection = connection
        self._lock = RLock()
        self._transaction_active = False
        self._closed = False
        raw = self._connection.connection.driver_connection
        features = set(_CORE_FEATURES)
        if callable(getattr(raw, "cancel", None)):
            features.add(DatabaseFeature.CANCELLATION)
        self._features = frozenset(features)

    @property
    def features(self) -> frozenset[DatabaseFeature]:
        return self._features

    def supports(self, feature: DatabaseFeature, /) -> bool:
        if not isinstance(feature, DatabaseFeature):
            raise TypeError("database feature is invalid")
        return feature in self._features

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
    ) -> AbstractContextManager[_PostgreSQLTransaction]:
        self._assert_open()
        if type(read_only) is not bool:
            raise ValueError("database transaction read-only flag must be boolean")
        return _PostgreSQLTransactionContext(self, read_only=read_only)

    def cancel(self) -> None:
        if self._closed:
            return
        raw = self._connection.connection.driver_connection
        cancel = getattr(raw, "cancel", None)
        if not callable(cancel):
            raise UnsupportedDatabaseFeature(DatabaseFeature.CANCELLATION)
        try:
            cancel()
        except Exception as error:
            raise _failure(error, stage="database.cancel") from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._transaction_active:
                raise DatabaseOperationError(
                    reason=PlatformReason.TRANSIENT_FAILURE,
                    stage="database.shutdown",
                )
            failures: list[BaseException] = []
            try:
                self._connection.close()
            except SQLAlchemyError as error:
                failures.append(error)
            try:
                self._engine.dispose()
            except SQLAlchemyError as error:
                failures.append(error)
            self._closed = True
            if failures:
                raise DatabaseOperationError(
                    reason=PlatformReason.SHUTDOWN_FAILED,
                    stage="database.shutdown",
                    protected_cause=failures[0],
                ) from None

    def __enter__(self) -> PostgreSQLDriver:
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
