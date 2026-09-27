from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from af.framework.database_manager import DatabaseManager
from af.framework.errors import PlatformReason
from af.framework.managers import CapabilityValidationError
from af.framework.relational import (
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseStatement,
)
from af.framework.unit_of_work import UnitOfWork
from af.infrastructure.sqlite import SQLiteDriver
from af.infrastructure.sqlite_manager import sqlite_registration

_NOW = datetime(2026, 7, 24, 8, tzinfo=UTC)


def _manager() -> DatabaseManager:
    manager = DatabaseManager(
        connections={
            "primary": {"driver": "sqlite", "database": ":memory:"},
            "replica": {"driver": "sqlite", "database": ":memory:"},
        },
        default="primary",
        read="replica",
        write="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(sqlite_registration(clock=lambda: _NOW))
    return manager


@pytest.mark.parametrize("limit", [0, 4_097, True])
def test_unit_of_work_rejects_invalid_callback_limits(limit: object) -> None:
    driver = SQLiteDriver(":memory:")

    with pytest.raises(ValueError, match="callback limit"):
        UnitOfWork(
            driver,
            max_after_commit_callbacks=cast(int, limit),
        )

    driver.close()


def test_unit_of_work_fails_closed_at_callback_limit_without_losing_registered_work() -> None:
    driver = SQLiteDriver(":memory:")
    observed: list[str] = []

    with UnitOfWork(driver, max_after_commit_callbacks=1) as unit:
        unit.after_commit(lambda: observed.append("first"))
        with pytest.raises(DatabaseOperationError) as captured:
            unit.after_commit(lambda: observed.append("overflow"))

    assert captured.value.reason is PlatformReason.VALIDATION_FAILED
    assert captured.value.stage == "database.after-commit.limit"
    assert observed == ["first"]
    assert unit.committed
    driver.close()


def test_database_manager_routes_unit_of_work_through_verified_read_and_write_drivers() -> None:
    manager = _manager()

    with manager.unit_of_work(requires=(DatabaseFeature.SAVEPOINTS,)) as write_unit:
        write_unit.transaction.execute(DatabaseStatement("CREATE TABLE records (value TEXT)"))
        write_unit.transaction.execute(
            DatabaseStatement(
                "INSERT INTO records VALUES (:value)",
                {"value": "committed"},
            )
        )

    with manager.unit_of_work(read_only=True) as read_unit:
        row = read_unit.transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))[0]

    assert row["value"] == 1
    assert write_unit.committed
    assert read_unit.committed
    assert manager.resolved_connections == ("primary", "replica")
    manager.close()


def test_database_manager_validates_unit_of_work_before_driver_resolution() -> None:
    manager = _manager()

    with pytest.raises(ValueError, match="read-only flag"):
        manager.unit_of_work(read_only=1)  # type: ignore[arg-type]
    with pytest.raises(CapabilityValidationError) as captured:
        manager.unit_of_work(requires=(DatabaseFeature.ADVISORY_LOCKS,))

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert manager.resolved_connections == ()
    manager.close()


def test_unit_of_work_cannot_hide_body_failure_suppressed_by_transaction_adapter():
    """Retain the historical UoW invariant: rollback is never successful completion."""
    from contextlib import contextmanager

    driver = SQLiteDriver(":memory:")
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE evidence (value INTEGER)"))

    class SuppressingDriver:
        @contextmanager
        def transaction(self, *, read_only=False):
            try:
                with driver.transaction(read_only=read_only) as transaction:
                    yield transaction
            except RuntimeError:
                return

    published = []
    try:
        with (
            pytest.raises(RuntimeError, match="body failed"),
            UnitOfWork(SuppressingDriver()) as unit,
        ):
            unit.transaction.execute(DatabaseStatement("INSERT INTO evidence VALUES (1)"))
            unit.after_commit(lambda: published.append("committed"))
            raise RuntimeError("body failed")
        assert not unit.committed and published == []
        with driver.transaction(read_only=True) as transaction:
            assert (
                transaction.query(DatabaseStatement("SELECT value FROM evidence", read_only=True))
                == ()
            )
    finally:
        driver.close()
