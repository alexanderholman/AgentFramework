"""Transaction-scoped UnitOfWork with post-commit publication hooks."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Literal

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseDriver,
    DatabaseOperationError,
    DatabaseTransaction,
)

__all__ = ("AfterCommitCallbackError", "UnitOfWork")

type AfterCommitCallback = Callable[[], object]


class AfterCommitCallbackError(DatabaseOperationError):
    """Raised after commit when one or more deferred callbacks fail."""

    def __init__(self, failures: tuple[Exception, ...]) -> None:
        if not failures:
            raise ValueError("after-commit failures must not be empty")
        self.failures = failures
        super().__init__(
            reason=PlatformReason.PERMANENT_FAILURE,
            stage="database.after-commit",
            protected_cause=failures[0],
        )


class UnitOfWork(AbstractContextManager["UnitOfWork"]):
    """One single-use database transaction plus ordered after-commit callbacks."""

    def __init__(self, driver: DatabaseDriver, *, read_only: bool = False) -> None:
        if type(read_only) is not bool:
            raise ValueError("unit of work read-only flag must be boolean")
        self._driver = driver
        self._read_only = read_only
        self._context: AbstractContextManager[DatabaseTransaction] | None = None
        self._transaction: DatabaseTransaction | None = None
        self._callbacks: list[AfterCommitCallback] = []
        self._active = False
        self._finished = False
        self._committed = False

    @property
    def transaction(self) -> DatabaseTransaction:
        """Return the transaction only while this unit is active."""

        transaction = self._transaction
        if not self._active or transaction is None:
            raise DatabaseOperationError(
                reason=PlatformReason.CONTEXT_UNAVAILABLE,
                stage="database.unit-of-work",
            )
        return transaction

    @property
    def committed(self) -> bool:
        """Report whether the authoritative transaction committed."""

        return self._committed

    def after_commit(self, callback: AfterCommitCallback, /) -> None:
        """Defer a callback until the transaction has committed successfully."""

        if not self._active:
            raise DatabaseOperationError(
                reason=PlatformReason.CONTEXT_UNAVAILABLE,
                stage="database.unit-of-work",
            )
        if not callable(callback):
            raise TypeError("after-commit callback must be callable")
        self._callbacks.append(callback)

    def __enter__(self) -> UnitOfWork:
        if self._active:
            raise DatabaseOperationError(
                reason=PlatformReason.TRANSIENT_FAILURE,
                stage="database.unit-of-work",
            )
        if self._finished:
            raise DatabaseOperationError(
                reason=PlatformReason.CONTEXT_UNAVAILABLE,
                stage="database.unit-of-work",
            )
        self._finished = True
        context = self._driver.transaction(read_only=self._read_only)
        try:
            transaction = context.__enter__()
        except BaseException:
            self._callbacks.clear()
            raise
        self._context = context
        self._transaction = transaction
        self._active = True
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        context = self._context
        if not self._active or context is None:
            raise DatabaseOperationError(
                reason=PlatformReason.CONTEXT_UNAVAILABLE,
                stage="database.unit-of-work",
            )

        callbacks: tuple[AfterCommitCallback, ...] = ()
        try:
            suppressed = context.__exit__(error_type, error, traceback)
            if error_type is None:
                self._committed = True
                callbacks = tuple(self._callbacks)
            return_value = bool(suppressed)
        finally:
            self._active = False
            self._transaction = None
            self._context = None
            self._callbacks.clear()

        if callbacks:
            failures: list[Exception] = []
            for callback in callbacks:
                try:
                    callback()
                except Exception as callback_error:
                    failures.append(callback_error)
            if failures:
                raise AfterCommitCallbackError(tuple(failures)) from None
        return return_value
