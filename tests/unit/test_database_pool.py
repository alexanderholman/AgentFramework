from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from af.framework.database_manager import DatabaseManager
from af.framework.database_pool import DatabasePool
from af.framework.errors import PlatformReason
from af.framework.managers import CapabilityValidationError, DriverResolutionError
from af.framework.relational import (
    DatabaseDriver,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseStatement,
    UnsupportedDatabaseFeature,
)
from af.infrastructure import sqlite as sqlite_module
from af.infrastructure.sqlite import SQLiteDriver
from af.infrastructure.sqlite_manager import sqlite_registration


def _pool(path: Path, *, size: int = 2, timeout: float = 0.1) -> DatabasePool:
    return DatabasePool(
        lambda: SQLiteDriver(path),
        size=size,
        acquire_timeout=timeout,
    )


class _FakeDriver:
    def __init__(
        self,
        *,
        features: frozenset[DatabaseFeature] | None = None,
        fail_transaction: bool = False,
        fail_cancel: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.features = (
            frozenset({DatabaseFeature.TRANSACTIONS, DatabaseFeature.CANCELLATION})
            if features is None
            else features
        )
        self.fail_transaction = fail_transaction
        self.fail_cancel = fail_cancel
        self.fail_close = fail_close
        self.closed = False

    def supports(self, feature: DatabaseFeature, /) -> bool:
        return feature in self.features

    def require(self, feature: DatabaseFeature, /) -> None:
        if not self.supports(feature):
            raise UnsupportedDatabaseFeature(feature)

    def transaction(self, *, read_only: bool = False):  # type: ignore[no-untyped-def]
        del read_only
        if self.fail_transaction:
            raise DatabaseOperationError(stage="database.test")
        return nullcontext(object())

    def cancel(self) -> None:
        if self.fail_cancel:
            raise RuntimeError("private cancellation failure")

    def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("private shutdown failure")


def test_database_pool_allows_bounded_parallel_real_sqlite_transactions(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path / "parallel.sqlite3")
    entered = threading.Barrier(3)
    release = threading.Event()
    results: list[int] = []

    def read() -> None:
        with pool.transaction(read_only=True) as transaction:
            entered.wait(timeout=2)
            results.append(
                transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))[0][
                    "value"
                ]
            )
            assert release.wait(timeout=2)

    workers = [threading.Thread(target=read) for _ in range(2)]
    for worker in workers:
        worker.start()
    entered.wait(timeout=2)
    assert pool.leased == 2
    release.set()
    for worker in workers:
        worker.join(timeout=2)

    assert not any(worker.is_alive() for worker in workers)
    assert results == [1, 1]
    assert pool.available == 2
    pool.close()


@pytest.mark.parametrize(
    ("factory", "size", "timeout"),
    [
        (None, 1, 1.0),
        (lambda: _FakeDriver(), 0, 1.0),
        (lambda: _FakeDriver(), True, 1.0),
        (lambda: _FakeDriver(), 1, 0),
        (lambda: _FakeDriver(), 1, True),
    ],
)
def test_database_pool_rejects_invalid_construction(
    factory,
    size,
    timeout,
) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        DatabasePool(factory, size=size, acquire_timeout=timeout)


def test_database_pool_rejects_invalid_or_inconsistent_drivers() -> None:
    with pytest.raises(DatabaseOperationError) as invalid:
        DatabasePool(lambda: object(), size=1, acquire_timeout=1)  # type: ignore[arg-type]
    assert invalid.value.stage == "database.pool.connect"

    first = _FakeDriver()
    second = _FakeDriver(
        features=frozenset(
            {
                DatabaseFeature.TRANSACTIONS,
                DatabaseFeature.CANCELLATION,
                DatabaseFeature.JSON,
            }
        )
    )
    drivers = iter((first, second))
    with pytest.raises(DatabaseOperationError):
        DatabasePool(
            lambda: next(drivers),  # type: ignore[arg-type]
            size=2,
            acquire_timeout=1,
        )
    assert first.closed
    assert second.closed

    without_cancel = _FakeDriver(features=frozenset({DatabaseFeature.TRANSACTIONS}))
    with pytest.raises(DatabaseOperationError):
        DatabasePool(
            lambda: without_cancel,  # type: ignore[arg-type]
            size=1,
            acquire_timeout=1,
        )
    assert without_cancel.closed


def test_database_pool_preserves_driver_connection_failure() -> None:
    failure = DatabaseOperationError(
        reason=PlatformReason.TRANSIENT_FAILURE,
        stage="database.connect",
    )

    def fail() -> DatabaseDriver:
        raise failure

    with pytest.raises(DatabaseOperationError) as captured:
        DatabasePool(fail, size=1, acquire_timeout=1)
    assert captured.value is failure


def test_database_pool_feature_and_context_contracts() -> None:
    driver = _FakeDriver()
    pool = DatabasePool(
        lambda: driver,  # type: ignore[arg-type]
        size=1,
        acquire_timeout=1,
    )

    assert pool.supports(DatabaseFeature.POOLING)
    assert pool.supports(DatabaseFeature.CANCELLATION)
    pool.require(DatabaseFeature.TRANSACTIONS)
    pool.cancel()
    with pytest.raises(UnsupportedDatabaseFeature):
        pool.require(DatabaseFeature.JSON)
    with pytest.raises(TypeError):
        pool.supports("pooling")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        pool.transaction(read_only=1)  # type: ignore[arg-type]
    with pool as entered:
        assert entered is pool
    assert driver.closed
    with pytest.raises(DatabaseOperationError), pool:
        pass


def test_database_pool_returns_lease_when_transaction_entry_fails() -> None:
    driver = _FakeDriver(fail_transaction=True)
    pool = DatabasePool(
        lambda: driver,  # type: ignore[arg-type]
        size=1,
        acquire_timeout=1,
    )

    with pytest.raises(DatabaseOperationError), pool.transaction():
        pass

    assert pool.available == 1
    assert pool.leased == 0
    pool.close()


def test_database_pool_aggregates_cancel_and_shutdown_failures() -> None:
    cancel_driver = _FakeDriver(fail_cancel=True)
    cancel_pool = DatabasePool(
        lambda: cancel_driver,  # type: ignore[arg-type]
        size=1,
        acquire_timeout=1,
    )
    with (
        cancel_pool.transaction(),
        pytest.raises(DatabaseOperationError) as captured,
    ):
        cancel_pool.cancel()
    assert captured.value.stage == "database.pool.cancel"
    cancel_pool.close()

    close_driver = _FakeDriver(fail_close=True)
    close_pool = DatabasePool(
        lambda: close_driver,  # type: ignore[arg-type]
        size=1,
        acquire_timeout=1,
    )
    with pytest.raises(DatabaseOperationError) as captured:
        close_pool.close()
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED
    assert captured.value.stage == "database.pool.shutdown"


def test_database_pool_times_out_when_capacity_is_exhausted(tmp_path: Path) -> None:
    pool = _pool(tmp_path / "timeout.sqlite3", size=1, timeout=0.01)

    with (
        pool.transaction(read_only=True),
        pytest.raises(DatabaseOperationError) as captured,
        pool.transaction(),
    ):
        pass

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert captured.value.stage == "database.pool.acquire"
    pool.close()


def test_database_pool_close_drains_active_lease_and_rejects_new_work(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path / "drain.sqlite3", size=1)

    with pool.transaction(read_only=True) as transaction:
        pool.close()
        assert (
            transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))[0]["value"]
            == 1
        )

    assert pool.available == 0
    assert pool.leased == 0
    pool.close()
    with pytest.raises(DatabaseOperationError) as captured, pool.transaction():
        pass
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_pool_cancellation_interrupts_real_sqlite_query_and_recovers(
    tmp_path: Path,
) -> None:
    pool = _pool(tmp_path / "cancel.sqlite3", size=1)
    started = threading.Event()
    finished = threading.Event()
    errors: list[DatabaseOperationError] = []

    def long_query() -> None:
        try:
            with pool.transaction(read_only=True) as transaction:
                started.set()
                transaction.query(
                    DatabaseStatement(
                        """
                        WITH RECURSIVE count(value) AS (
                            SELECT 1
                            UNION ALL
                            SELECT value + 1 FROM count WHERE value < 100000000
                        )
                        SELECT sum(value) AS value FROM count
                        """,
                        read_only=True,
                    )
                )
        except DatabaseOperationError as error:
            errors.append(error)
        finally:
            finished.set()

    worker = threading.Thread(target=long_query)
    worker.start()
    assert started.wait(timeout=2)
    time.sleep(0.05)
    pool.cancel()
    assert finished.wait(timeout=2)
    worker.join(timeout=2)

    assert len(errors) == 1
    assert errors[0].reason is PlatformReason.CANCELLED
    assert errors[0].stage == "database.query"
    with pool.transaction(read_only=True) as transaction:
        assert (
            transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))[0]["value"]
            == 1
        )
    pool.close()


def test_sqlite_pool_registration_reports_only_verified_runtime_capabilities(
    tmp_path: Path,
) -> None:
    manager = DatabaseManager(
        connections={
            "primary": {
                "driver": "sqlite",
                "database": str(tmp_path / "managed.sqlite3"),
                "pool_size": 2,
                "pool_timeout": 0.1,
            }
        },
        default="primary",
        read="primary",
        write="primary",
        deployment_profile="test",
        policy_version="policy-v1",
    )
    manager.install(sqlite_registration())

    driver = manager.connection(
        requires=(
            DatabaseFeature.POOLING,
            DatabaseFeature.CANCELLATION,
            DatabaseFeature.TRANSACTIONAL_DDL,
        )
    )

    assert isinstance(driver, DatabasePool)
    assert driver.supports(DatabaseFeature.POOLING)
    assert driver.supports(DatabaseFeature.CANCELLATION)
    assert driver.supports(DatabaseFeature.TRANSACTIONAL_DDL)
    assert manager.evidence().capabilities.supports(
        (
            DatabaseFeature.POOLING.value,
            DatabaseFeature.CANCELLATION.value,
            DatabaseFeature.TRANSACTIONAL_DDL.value,
        )
    )
    manager.close()


def test_sqlite_manifest_identity_includes_af_driver_and_runtime_bytes() -> None:
    driver_file = sqlite_module.__file__
    if driver_file is None:
        raise AssertionError("SQLite driver source identity is unavailable")
    expected = hashlib.sha256(
        Path(driver_file).read_bytes() + b"\0" + Path(sqlite3.__file__).read_bytes()
    ).hexdigest()

    assert sqlite_registration().manifest.artifact_digest == expected


def test_unpooled_sqlite_evidence_does_not_claim_pooling() -> None:
    manager = DatabaseManager(
        connections={
            "primary": {
                "driver": "sqlite",
                "database": ":memory:",
            }
        },
        default="primary",
        read="primary",
        write="primary",
        deployment_profile="test",
        policy_version="policy-v1",
    )
    manager.install(sqlite_registration())

    driver = manager.connection()

    assert isinstance(driver, SQLiteDriver)
    assert driver.supports(DatabaseFeature.TRANSACTIONAL_DDL)
    assert manager.evidence().capabilities.supports((DatabaseFeature.TRANSACTIONAL_DDL.value,))
    assert not driver.supports(DatabaseFeature.POOLING)
    assert not manager.evidence().capabilities.supports((DatabaseFeature.POOLING.value,))
    with pytest.raises(CapabilityValidationError):
        manager.connection(requires=(DatabaseFeature.POOLING,))
    manager.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"pool_size": 0},
        {"pool_size": True},
        {"pool_size": 2, "pool_timeout": 0},
        {"pool_size": 2, "pool_timeout": True},
        {"pool_size": 2, "extra": "unsafe"},
    ],
)
def test_sqlite_pool_configuration_fails_closed(
    tmp_path: Path,
    extra: dict[str, object],
) -> None:
    manager = DatabaseManager(
        connections={
            "primary": {
                "driver": "sqlite",
                "database": str(tmp_path / "invalid.sqlite3"),
                **extra,
            }
        },
        default="primary",
        read="primary",
        write="primary",
        deployment_profile="test",
        policy_version="policy-v1",
    )
    manager.install(sqlite_registration())

    with pytest.raises(DriverResolutionError):
        manager.connection()
    manager.close()


def test_sqlite_connection_failure_is_sanitized(tmp_path: Path) -> None:
    parent = tmp_path / "not-a-directory"
    parent.write_text("private")
    private_path = parent / "database.sqlite3"

    with pytest.raises(DatabaseOperationError) as captured:
        SQLiteDriver(private_path)

    assert captured.value.stage == "database.connect"
    assert str(private_path) not in str(captured.value)
