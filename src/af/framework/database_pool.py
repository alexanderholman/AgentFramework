"""Bounded driver-neutral relational connection pooling."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from threading import Condition, RLock
from time import monotonic
from types import TracebackType
from typing import Literal

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseDriver,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseTransaction,
    UnsupportedDatabaseFeature,
)

__all__ = ("DatabasePool",)

_MAX_POOL_SIZE = 256
_MAX_ACQUIRE_TIMEOUT = 3_600.0


class _PooledTransactionContext(AbstractContextManager[DatabaseTransaction]):
    def __init__(self, pool: DatabasePool, *, read_only: bool) -> None:
        self._pool = pool
        self._read_only = read_only
        self._driver: DatabaseDriver | None = None
        self._context: AbstractContextManager[DatabaseTransaction] | None = None

    def __enter__(self) -> DatabaseTransaction:
        driver = self._pool._acquire()
        try:
            context = driver.transaction(read_only=self._read_only)
            transaction = context.__enter__()
        except BaseException:
            self._pool._release(driver)
            raise
        self._driver = driver
        self._context = context
        return transaction

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        driver = self._driver
        context = self._context
        if driver is None or context is None:
            return None
        try:
            return context.__exit__(error_type, error, traceback)
        finally:
            self._driver = None
            self._context = None
            self._pool._release(driver)


class DatabasePool(AbstractContextManager["DatabasePool"]):
    """Eager finite pool with timed leases, cancellation, and graceful drain."""

    def __init__(
        self,
        factory: Callable[[], DatabaseDriver],
        *,
        size: int,
        acquire_timeout: float,
    ) -> None:
        if not callable(factory):
            raise ValueError("database pool factory must be callable")
        if type(size) is not int or not 1 <= size <= _MAX_POOL_SIZE:
            raise ValueError("database pool size is invalid")
        if (
            type(acquire_timeout) not in {int, float}
            or not 0 < float(acquire_timeout) <= _MAX_ACQUIRE_TIMEOUT
        ):
            raise ValueError("database pool acquire timeout is invalid")

        drivers: list[DatabaseDriver] = []
        driver_ids: set[int] = set()
        try:
            for _ in range(size):
                driver = factory()
                if not self._is_driver(driver):
                    raise TypeError("database pool factory returned an invalid driver")
                driver_id = id(driver)
                if driver_id in driver_ids:
                    raise ValueError("database pool factory returned a duplicate driver instance")
                drivers.append(driver)
                driver_ids.add(driver_id)
            base_features = drivers[0].features
            if any(driver.features != base_features for driver in drivers[1:]):
                raise ValueError("database pool drivers expose inconsistent capabilities")
            if DatabaseFeature.CANCELLATION not in base_features:
                raise ValueError("database pool drivers must support cancellation")
        except Exception as error:
            for driver in reversed(drivers):
                with suppress(BaseException):
                    driver.close()
            if isinstance(error, DatabaseOperationError):
                raise
            raise DatabaseOperationError(
                stage="database.pool.connect",
                protected_cause=error,
            ) from error

        self._features = frozenset({*base_features, DatabaseFeature.POOLING})
        self._available: deque[DatabaseDriver] = deque(drivers)
        self._leased: dict[int, DatabaseDriver] = {}
        self._acquire_timeout = float(acquire_timeout)
        self._closed = False
        self._condition = Condition(RLock())

    @staticmethod
    def _is_driver(value: object) -> bool:
        return all(
            callable(getattr(value, name, None))
            for name in ("supports", "require", "transaction", "cancel", "close")
        ) and isinstance(getattr(value, "features", None), frozenset)

    @property
    def features(self) -> frozenset[DatabaseFeature]:
        return self._features

    @property
    def available(self) -> int:
        with self._condition:
            return len(self._available)

    @property
    def leased(self) -> int:
        with self._condition:
            return len(self._leased)

    def supports(self, feature: DatabaseFeature, /) -> bool:
        if not isinstance(feature, DatabaseFeature):
            raise TypeError("database feature is invalid")
        return feature in self._features

    def require(self, feature: DatabaseFeature, /) -> None:
        if not self.supports(feature):
            raise UnsupportedDatabaseFeature(feature)

    def transaction(
        self,
        *,
        read_only: bool = False,
    ) -> AbstractContextManager[DatabaseTransaction]:
        if type(read_only) is not bool:
            raise ValueError("database transaction read-only flag must be boolean")
        with self._condition:
            self._assert_open()
        return _PooledTransactionContext(self, read_only=read_only)

    def cancel(self) -> None:
        """Interrupt all currently leased operations without closing the pool."""

        with self._condition:
            drivers = tuple(self._leased.values())
        failures: list[Exception] = []
        for driver in drivers:
            try:
                driver.cancel()
            except Exception as error:
                failures.append(error)
        if failures:
            raise DatabaseOperationError(
                stage="database.pool.cancel",
                protected_cause=ExceptionGroup(
                    "one or more database cancellations failed",
                    failures,
                ),
            )

    def close(self) -> None:
        """Reject new leases, close idle drivers, and close active drivers on return."""

        with self._condition:
            if self._closed:
                return
            self._closed = True
            idle = tuple(self._available)
            self._available.clear()
            self._condition.notify_all()
        self._close_drivers(idle)

    def _assert_open(self) -> None:
        if self._closed:
            raise DatabaseOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="database.pool",
            )

    def _acquire(self) -> DatabaseDriver:
        deadline = monotonic() + self._acquire_timeout
        with self._condition:
            while not self._available:
                self._assert_open()
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise DatabaseOperationError(
                        reason=PlatformReason.TRANSIENT_FAILURE,
                        stage="database.pool.acquire",
                    )
                self._condition.wait(remaining)
            self._assert_open()
            driver = self._available.popleft()
            self._leased[id(driver)] = driver
            return driver

    def _release(self, driver: DatabaseDriver) -> None:
        with self._condition:
            if self._leased.pop(id(driver), None) is not driver:
                raise DatabaseOperationError(
                    reason=PlatformReason.INTEGRITY_FAILURE,
                    stage="database.pool.release",
                )
            closing = self._closed
            if not closing:
                self._available.append(driver)
            self._condition.notify()
        if closing:
            self._close_drivers((driver,))

    @staticmethod
    def _close_drivers(drivers: tuple[DatabaseDriver, ...]) -> None:
        failures: list[Exception] = []
        for driver in reversed(drivers):
            try:
                driver.close()
            except Exception as error:
                failures.append(error)
        if failures:
            raise DatabaseOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="database.pool.shutdown",
                protected_cause=ExceptionGroup(
                    "one or more database drivers failed to close",
                    failures,
                ),
            )

    def __enter__(self) -> DatabasePool:
        with self._condition:
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
