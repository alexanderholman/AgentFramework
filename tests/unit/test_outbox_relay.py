from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from af.framework.errors import PlatformReason
from af.framework.relational import DatabaseStatement
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata, TransactionalOutbox
from af.messaging.relay import (
    OutboxBatchReport,
    OutboxPublishError,
    OutboxRelay,
    OutboxRelayError,
    OutboxRelayLeaseUnavailable,
    OutboxRetryPolicy,
)

_NOW = datetime(2026, 7, 24, 10, tzinfo=UTC)


class MutableClock:
    def __init__(self, value: datetime = _NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class RecordingPublisher:
    def __init__(self) -> None:
        self.message_ids: list[str] = []

    def publish(self, envelope: MessageEnvelope, /) -> None:
        self.message_ids.append(envelope.metadata.message_id)


class ScriptedPublisher:
    def __init__(self, failures: dict[str, list[Exception]]) -> None:
        self._failures = failures
        self.message_ids: list[str] = []

    def publish(self, envelope: MessageEnvelope, /) -> None:
        message_id = envelope.metadata.message_id
        self.message_ids.append(message_id)
        failures = self._failures.get(message_id)
        if failures:
            raise failures.pop(0)


class StealingPublisher:
    def __init__(self, clock: MutableClock, competitor: OutboxRelay) -> None:
        self._clock = clock
        self._competitor = competitor
        self.called = False

    def publish(self, envelope: MessageEnvelope, /) -> None:
        del envelope
        self.called = True
        self._clock.advance(timedelta(minutes=2))
        with self._competitor.lock():
            pass


def _event(message_id: str, *, occurred_at: datetime = _NOW) -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=message_id,
            message_type="records.changed",
            schema_version=1,
            kind=EnvelopeKind.INTEGRATION_EVENT,
            occurred_at=occurred_at,
        ),
        payload={"message_id": message_id, "secret": "not-for-relay-evidence"},
    )


def _relay(
    driver: SQLiteDriver,
    publisher: RecordingPublisher | ScriptedPublisher,
    clock: MutableClock,
    *,
    owner: str = "relay-a",
    policy: OutboxRetryPolicy | None = None,
    lease: timedelta = timedelta(minutes=5),
) -> tuple[OutboxRelay, TransactionalOutbox]:
    outbox = TransactionalOutbox()
    relay = OutboxRelay(
        driver,
        publisher,
        owner_id=owner,
        clock=clock,
        outbox=outbox,
        retry_policy=policy,
        lease=lease,
    )
    relay.install()
    relay.install()
    return relay, outbox


def _append(
    driver: SQLiteDriver,
    outbox: TransactionalOutbox,
    *events: MessageEnvelope,
    available_at: datetime | None = None,
) -> None:
    with driver.transaction() as transaction:
        for event in events:
            outbox.append(transaction, event, available_at=available_at)


def _states(driver: SQLiteDriver) -> tuple[tuple[object, ...], ...]:
    with driver.transaction(read_only=True) as transaction:
        rows = transaction.query(
            DatabaseStatement(
                """SELECT message_id, attempt_count, available_at, published_at,
                dead_lettered_at, last_error_code
                FROM af_outbox ORDER BY message_id""",
                read_only=True,
            )
        )
    return tuple(row.values for row in rows)


def test_relay_publishes_one_due_batch_in_deterministic_order() -> None:
    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    publisher = RecordingPublisher()
    relay, outbox = _relay(driver, publisher, clock)
    due = _NOW + timedelta(seconds=1)
    future = _NOW + timedelta(minutes=1)
    _append(driver, outbox, _event("event-b"), _event("event-a"), available_at=due)
    _append(driver, outbox, _event("event-future"), available_at=future)
    clock.value = due

    report = relay.run_once(limit=2)

    assert report == OutboxBatchReport(selected=2, published=2, retried=0, dead_lettered=0)
    assert publisher.message_ids == ["event-a", "event-b"]
    assert "not-for-relay-evidence" not in repr(report)
    assert relay.run_once().selected == 0
    clock.value = future
    assert relay.run_once().published == 1
    assert publisher.message_ids == ["event-a", "event-b", "event-future"]
    driver.close()


def test_typed_failures_retry_then_dead_letter_under_finite_policy() -> None:
    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    publisher = ScriptedPublisher(
        {
            "event-retry": [
                OutboxPublishError("transport.retryable", retryable=True),
                OutboxPublishError("transport.retryable", retryable=True),
            ],
            "event-terminal": [
                OutboxPublishError("transport.rejected", retryable=False),
            ],
        }
    )
    policy = OutboxRetryPolicy(max_attempts=2, retry_delay=timedelta(minutes=1))
    relay, outbox = _relay(driver, publisher, clock, policy=policy)
    _append(driver, outbox, _event("event-retry"), _event("event-terminal"))

    first = relay.run_once()

    assert first == OutboxBatchReport(selected=2, published=0, retried=1, dead_lettered=1)
    assert _states(driver) == (
        (
            "event-retry",
            1,
            "2026-07-24T10:01:00.000000Z",
            None,
            None,
            "transport.retryable",
        ),
        (
            "event-terminal",
            1,
            "2026-07-24T10:00:00.000000Z",
            None,
            "2026-07-24T10:00:00.000000Z",
            "transport.rejected",
        ),
    )
    clock.advance(timedelta(minutes=1))
    second = relay.run_once()
    assert second == OutboxBatchReport(selected=1, published=0, retried=0, dead_lettered=1)
    assert _states(driver)[0][4] == "2026-07-24T10:01:00.000000Z"
    assert _states(driver)[0][5] == "transport.retryable"
    driver.close()


def test_untyped_publisher_failure_is_sanitized_and_scheduled_for_retry() -> None:
    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    publisher = ScriptedPublisher({"event-secret": [RuntimeError("secret transport state")]})
    policy = OutboxRetryPolicy(max_attempts=3, retry_delay=timedelta(seconds=30))
    relay, outbox = _relay(driver, publisher, clock, policy=policy)
    _append(driver, outbox, _event("event-secret"))

    with pytest.raises(OutboxRelayError) as captured:
        relay.run_once()

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert "secret transport state" not in str(captured.value)
    assert "secret transport state" not in repr(captured.value)
    clock.advance(timedelta(seconds=30))
    with driver.transaction(read_only=True) as transaction:
        pending = outbox.pending(transaction, now=clock())
    assert len(pending) == 1
    assert pending[0].attempt_count == 1
    assert pending[0].last_error_code == "publisher.failure"
    driver.close()


def test_relay_lease_excludes_competitors_and_renewal_extends_ownership(tmp_path: Path) -> None:
    database = tmp_path / "outbox-relay.sqlite3"
    clock = MutableClock()
    first_driver = SQLiteDriver(database)
    second_driver = SQLiteDriver(database)
    first, _ = _relay(
        first_driver,
        RecordingPublisher(),
        clock,
        owner="relay-a",
        lease=timedelta(minutes=1),
    )
    second, _ = _relay(
        second_driver,
        RecordingPublisher(),
        clock,
        owner="relay-b",
        lease=timedelta(minutes=1),
    )

    with first.lock() as lease:
        clock.advance(timedelta(seconds=30))
        lease.renew()
        clock.advance(timedelta(seconds=40))
        with pytest.raises(OutboxRelayLeaseUnavailable) as captured, second.lock():
            pass
        assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE

    with second.lock():
        pass
    first_driver.close()
    second_driver.close()


def test_expired_stolen_lease_prevents_stale_publication_acknowledgement(tmp_path: Path) -> None:
    database = tmp_path / "outbox-relay-stolen.sqlite3"
    clock = MutableClock()
    first_driver = SQLiteDriver(database)
    second_driver = SQLiteDriver(database)
    competitor, _ = _relay(
        second_driver,
        RecordingPublisher(),
        clock,
        owner="relay-b",
        lease=timedelta(minutes=1),
    )
    outbox = TransactionalOutbox()
    stealing = StealingPublisher(clock, competitor)
    first = OutboxRelay(
        first_driver,
        stealing,
        owner_id="relay-a",
        clock=clock,
        outbox=outbox,
        lease=timedelta(minutes=1),
    )
    first.install()
    _append(first_driver, outbox, _event("event-at-least-once"))

    with pytest.raises(OutboxRelayLeaseUnavailable):
        first.run_once()

    assert stealing.called
    with first_driver.transaction(read_only=True) as transaction:
        pending = outbox.pending(transaction, now=clock())
    assert tuple(item.envelope.metadata.message_id for item in pending) == ("event-at-least-once",)
    first_driver.close()
    second_driver.close()


def test_relay_contracts_reject_ambiguous_or_unbounded_inputs() -> None:
    with pytest.raises(ValueError, match="maximum attempts"):
        OutboxRetryPolicy(max_attempts=cast(int, True))
    with pytest.raises(ValueError, match="retry delay"):
        OutboxRetryPolicy(retry_delay=timedelta(0))
    with pytest.raises(ValueError, match="do not reconcile"):
        OutboxBatchReport(selected=1, published=0, retried=0, dead_lettered=0)
    with pytest.raises(ValueError, match="canonical identifier"):
        OutboxPublishError("INVALID CODE", retryable=True)
    with pytest.raises(ValueError, match="retryable flag"):
        OutboxPublishError("transport.failed", retryable=cast(bool, 1))

    driver = SQLiteDriver(":memory:")
    clock = MutableClock()
    relay, _ = _relay(driver, RecordingPublisher(), clock)
    with pytest.raises(ValueError, match="batch limit"):
        relay.run_once(limit=cast(int, True))
    with pytest.raises(ValueError, match="batch limit"):
        relay.run_once(limit=0)
    with pytest.raises(TypeError, match="publisher"):
        OutboxRelay(driver, cast(RecordingPublisher, object()), owner_id="relay", clock=clock)
    with pytest.raises(ValueError, match="owner id"):
        OutboxRelay(
            driver,
            RecordingPublisher(),
            owner_id="INVALID OWNER",
            clock=clock,
        )
    driver.close()
