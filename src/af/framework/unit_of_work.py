"""Explicit relational unit-of-work and after-commit lifecycle semantics."""

from __future__ import annotations

from asyncio import CancelledError
from collections.abc import Callable
from contextlib import AbstractContextManager
from enum import StrEnum
from types import TracebackType
from typing import Literal

from af.framework.errors import PlatformError, PlatformReason
from af.framework.relational import DatabaseTransaction

__all__ = (
    "UnitOfWork",
    "UnitOfWorkAfterCommitError",
    "UnitOfWorkLifecycleError",
    "UnitOfWorkState",
)

_MAX_CALLBACK_LIMIT = 4_096


class UnitOfWorkState(StrEnum):
    """Observable terminal states without exposing transaction internals."""

    NEW = "new"
    ACTIVE = "active"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled-back"
    AFTER_COMMIT_FAILED = "after-commit-failed"
    FAILED = "failed"


class UnitOfWorkLifecycleError(PlatformError):
    """The unit-of-work lifecycle was used inconsistently or suppressed an error."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            PlatformReason.INTEGRITY_FAILURE,
            stage="unit-of-work.lifecycle",
            protected_cause=protected_cause,
        )


class UnitOfWorkAfterCommitError(PlatformError):
    """A non-durable callback failed after the authoritative commit completed."""

    def __init__(self, callback_index: int, *, protected_cause: BaseException) -> None:
        self.callback_index = callback_index
        super().__init__(
            PlatformReason.PERMANENT_FAILURE,
            stage="unit-of-work.after-commit",
            protected_cause=protected_cause,
        )


class UnitOfWork(AbstractContextManager[DatabaseTransaction]):
    """Own one database transaction plus bounded post-commit callbacks.

    Callbacks are intentionally non-durable. They run in registration order only after the
    wrapped transaction context has committed successfully. A rollback discards them. Durable
    cross-system publication must use the transactional outbox rather than relying on callbacks.
    """

    def __init__(
        self,
        transaction_context: AbstractContextManager[DatabaseTransaction],
        *,
        max_after_commit_callbacks: int = 1_024,
    ) -> None:
        if (
            type(max_after_commit_callbacks) is not int
            or not 1 <= max_after_commit_callbacks <= _MAX_CALLBACK_LIMIT
        ):
            raise ValueError("unit-of-work callback limit is invalid")
        self._transaction_context = transaction_context
        self._max_after_commit_callbacks = max_after_commit_callbacks
        self._callbacks: list[Callable[[], None]] = []
        self._transaction: DatabaseTransaction | None = None
        self._state = UnitOfWorkState.NEW

    @property
    def state(self) -> UnitOfWorkState:
        return self._state

    @property
    def transaction(self) -> DatabaseTransaction:
        if self._state is not UnitOfWorkState.ACTIVE or self._transaction is None:
            raise UnitOfWorkLifecycleError()
        return self._transaction

    def after_commit(self, callback: Callable[[], None], /) -> None:
        """Register one synchronous callback while the transaction is active."""

        if self._state is not UnitOfWorkState.ACTIVE:
            raise UnitOfWorkLifecycleError()
        if not callable(callback):
            raise TypeError("after-commit callback must be callable")
        if len(self._callbacks) >= self._max_after_commit_callbacks:
            raise UnitOfWorkLifecycleError()
        self._callbacks.append(callback)

    def __enter__(self) -> DatabaseTransaction:
        if self._state is not UnitOfWorkState.NEW:
            raise UnitOfWorkLifecycleError()
        try:
            transaction = self._transaction_context.__enter__()
        except BaseException:
            self._state = UnitOfWorkState.FAILED
            raise
        self._transaction = transaction
        self._state = UnitOfWorkState.ACTIVE
        return transaction

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        if self._state is not UnitOfWorkState.ACTIVE:
            raise UnitOfWorkLifecycleError()
        try:
            suppressed = self._transaction_context.__exit__(error_type, error, traceback)
        except BaseException:
            self._callbacks.clear()
            self._transaction = None
            self._state = UnitOfWorkState.FAILED
            raise

        self._transaction = None
        if suppressed:
            self._callbacks.clear()
            self._state = UnitOfWorkState.FAILED
            raise UnitOfWorkLifecycleError(protected_cause=error)
        if error_type is not None:
            self._callbacks.clear()
            self._state = UnitOfWorkState.ROLLED_BACK
            return False

        callbacks = tuple(self._callbacks)
        self._callbacks.clear()
        self._state = UnitOfWorkState.COMMITTED
        for index, callback in enumerate(callbacks):
            try:
                result = callback()
                if result is not None:
                    raise TypeError("after-commit callback must return None")
            except (CancelledError, KeyboardInterrupt, SystemExit):
                self._state = UnitOfWorkState.AFTER_COMMIT_FAILED
                raise
            except BaseException as callback_error:
                self._state = UnitOfWorkState.AFTER_COMMIT_FAILED
                raise UnitOfWorkAfterCommitError(
                    index,
                    protected_cause=callback_error,
                ) from None
        return False
