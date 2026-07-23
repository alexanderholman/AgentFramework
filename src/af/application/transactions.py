"""Manager-extensible transaction middleware for application dispatch."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import TracebackType
from typing import Protocol

from af.application.bus import (
    ApplicationResult,
    CallNext,
    DispatchRequest,
    InvalidDispatchContract,
)
from af.framework.errors import PlatformError, PlatformReason

__all__ = ("TransactionBoundary", "TransactionMiddleware")


class TransactionBoundary(Protocol):
    """One async transaction supplied by a manager or application adapter."""

    async def __aenter__(self) -> object: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


TransactionResolver = Callable[[DispatchRequest], TransactionBoundary | None]


class TransactionMiddleware:
    """Wrap selected dispatches in one manager-provided transaction boundary."""

    def __init__(self, resolve: TransactionResolver) -> None:
        if not callable(resolve):
            raise InvalidDispatchContract("transaction resolver must be callable")
        self._resolve = resolve

    async def __call__(
        self,
        request: DispatchRequest,
        call_next: CallNext,
    ) -> ApplicationResult[object]:
        try:
            transaction = self._resolve(request)
        except asyncio.CancelledError:
            raise
        except PlatformError:
            raise
        except Exception as error:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="application.transaction-resolution",
                protected_cause=error,
            ) from error
        if transaction is None:
            return await call_next(request)
        if not callable(getattr(transaction, "__aenter__", None)) or not callable(
            getattr(transaction, "__aexit__", None)
        ):
            raise InvalidDispatchContract("transaction resolver returned an invalid async boundary")
        async with transaction:
            return await call_next(request)
