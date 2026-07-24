from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.framework.errors import PlatformReason
from af.framework.migrations import (
    MigrationIntegrityError,
    MigrationLockUnavailable,
    MigrationRunner,
)
from af.framework.relational import DatabaseOperationError, DatabaseStatement
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import MessageEnvelope
from af.messaging.relay import (
    OutboxRelay,
    OutboxRelayIntegrityError,
    OutboxRelayLeaseUnavailable,
)

_NOW = datetime(2026, 7, 24, 10, tzinfo=UTC)


class MutableClock:
    def __init__(self) -> None:
        self.value = _NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class NoOpPublisher:
    def publish(self, envelope: MessageEnvelope, /) -> None:
        del envelope


def _relay(
    driver: SQLiteDriver,
    clock: MutableClock,
    *,
    owner: str,
) -> OutboxRelay:
    relay = OutboxRelay(
        driver,
        NoOpPublisher(),
        owner_id=owner,
        clock=clock,
        lease=timedelta(minutes=1),
    )
    relay.install()
    return relay


def test_released_intermediate_owner_does_not_reset_relay_generation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "outbox-relay-release-aba.sqlite3"
    clock = MutableClock()
    first_driver = SQLiteDriver(database)
    second_driver = SQLiteDriver(database)
    stale_relay = _relay(first_driver, clock, owner="shared-owner")
    intermediate = _relay(second_driver, clock, owner="other-owner")
    reincarnated = _relay(second_driver, clock, owner="shared-owner")

    stale = stale_relay.lock()
    stale.__enter__()
    clock.advance(timedelta(minutes=2))
    with intermediate.lock():
        pass

    with reincarnated.lock() as current:
        with pytest.raises(OutboxRelayLeaseUnavailable):
            stale.renew()
        current.renew()

    first_driver.close()
    second_driver.close()


def test_released_fast_clock_lease_is_immediately_reclaimable_by_slow_clock(
    tmp_path: Path,
) -> None:
    database = tmp_path / "outbox-relay-release-clock-skew.sqlite3"
    fast_clock = MutableClock()
    fast_clock.advance(timedelta(hours=1))
    slow_clock = MutableClock()
    first_driver = SQLiteDriver(database)
    second_driver = SQLiteDriver(database)
    fast_relay = _relay(first_driver, fast_clock, owner="fast-owner")
    slow_relay = _relay(second_driver, slow_clock, owner="slow-owner")

    with fast_relay.lock():
        pass

    with slow_relay.lock():
        pass

    first_driver.close()
    second_driver.close()


def test_migration_renewal_preserves_impossible_row_count_as_integrity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    runner = MigrationRunner(
        driver,
        (),
        owner_id="integrity-runner",
        clock=clock,
        lock_lease=timedelta(minutes=1),
    )
    runner.bootstrap()
    with driver.transaction() as transaction:
        transaction_type = type(transaction)
    original_execute = transaction_type.execute

    def impossible_renewal_count(
        transaction: object,
        statement: DatabaseStatement,
    ) -> int:
        if "next_expires_at" in statement.parameters:
            return 2
        return original_execute(transaction, statement)

    monkeypatch.setattr(transaction_type, "execute", impossible_renewal_count)
    with runner.lock() as lease, pytest.raises(MigrationIntegrityError) as captured:
        lease.renew()

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    driver.close()


def test_relay_renewal_preserves_impossible_row_count_as_integrity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    relay = _relay(driver, clock, owner="integrity-relay")
    with driver.transaction() as transaction:
        transaction_type = type(transaction)
    original_execute = transaction_type.execute

    def impossible_renewal_count(
        transaction: object,
        statement: DatabaseStatement,
    ) -> int:
        if "renewal" in statement.parameters and "now" in statement.parameters:
            return 2
        return original_execute(transaction, statement)

    monkeypatch.setattr(transaction_type, "execute", impossible_renewal_count)
    with relay.lock() as lease, pytest.raises(OutboxRelayIntegrityError) as captured:
        lease.renew()

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    driver.close()


def test_migration_renewal_sanitizes_driver_failure_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    runner = MigrationRunner(
        driver,
        (),
        owner_id="failing-runner",
        clock=clock,
        lock_lease=timedelta(minutes=1),
    )
    runner.bootstrap()
    with driver.transaction() as transaction:
        transaction_type = type(transaction)
    original_execute = transaction_type.execute

    def fail_renewal(
        transaction: object,
        statement: DatabaseStatement,
    ) -> int:
        if "next_expires_at" in statement.parameters:
            raise DatabaseOperationError(protected_cause=RuntimeError("secret database state"))
        return original_execute(transaction, statement)

    monkeypatch.setattr(transaction_type, "execute", fail_renewal)
    with runner.lock() as lease, pytest.raises(MigrationLockUnavailable) as captured:
        lease.renew()

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "secret database state" not in str(captured.value)
    assert "secret database state" not in repr(captured.value)
    driver.close()


def test_relay_renewal_sanitizes_driver_failure_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    relay = _relay(driver, clock, owner="failing-relay")
    with driver.transaction() as transaction:
        transaction_type = type(transaction)
    original_execute = transaction_type.execute

    def fail_renewal(
        transaction: object,
        statement: DatabaseStatement,
    ) -> int:
        if "renewal" in statement.parameters and "now" in statement.parameters:
            raise DatabaseOperationError(protected_cause=RuntimeError("secret database state"))
        return original_execute(transaction, statement)

    monkeypatch.setattr(transaction_type, "execute", fail_renewal)
    with relay.lock() as lease, pytest.raises(OutboxRelayLeaseUnavailable) as captured:
        lease.renew()

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "secret database state" not in str(captured.value)
    assert "secret database state" not in repr(captured.value)
    driver.close()
