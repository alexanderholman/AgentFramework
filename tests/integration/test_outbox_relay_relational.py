from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest

from af.framework.relational import DatabaseDriver, DatabaseStatement
from af.infrastructure.mariadb import MariaDBDriver
from af.infrastructure.mysql import MySQLDriver
from af.infrastructure.postgresql import PostgreSQLDriver
from af.messaging import (
    EnvelopeKind,
    MessageEnvelope,
    MessageMetadata,
    OutboxPublishError,
    OutboxRelay,
    OutboxRelayLeaseUnavailable,
    OutboxRetryPolicy,
    TransactionalOutbox,
)

_NOW = datetime(2026, 7, 24, 10, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.value = _NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class _Publisher:
    def __init__(self, retry_message_id: str) -> None:
        self._retry_message_id = retry_message_id
        self._failed = False
        self.message_ids: list[str] = []

    def publish(self, envelope: MessageEnvelope, /) -> None:
        message_id = envelope.metadata.message_id
        self.message_ids.append(message_id)
        if message_id == self._retry_message_id and not self._failed:
            self._failed = True
            raise OutboxPublishError("transport.retryable", retryable=True)


class _RecordingPublisher:
    def publish(self, envelope: MessageEnvelope, /) -> None:
        del envelope


def _event(backend: str, suffix: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=f"relay-{backend}-{suffix}",
            message_type="relay.conformance",
            schema_version=1,
            kind=EnvelopeKind.INTEGRATION_EVENT,
            occurred_at=_NOW,
        ),
        payload={"backend": backend, "suffix": suffix},
    )


def _exercise(
    first: DatabaseDriver,
    second: DatabaseDriver,
    backend: str,
) -> None:
    clock = _Clock()
    retry_event = _event(backend, "retry")
    publisher = _Publisher(retry_event.metadata.message_id)
    outbox = TransactionalOutbox()
    relay = OutboxRelay(
        first,
        publisher,
        owner_id=f"{backend}-relay-a",
        clock=clock,
        outbox=outbox,
        retry_policy=OutboxRetryPolicy(
            max_attempts=3,
            retry_delay=timedelta(seconds=1),
        ),
        relay_name=f"{backend}-conformance",
        lease=timedelta(minutes=1),
    )
    competitor = OutboxRelay(
        second,
        _RecordingPublisher(),
        owner_id=f"{backend}-relay-b",
        clock=clock,
        relay_name=f"{backend}-conformance",
        lease=timedelta(minutes=1),
    )
    relay.install()
    competitor.install()
    with first.transaction() as transaction:
        transaction.execute(DatabaseStatement("DELETE FROM af_outbox_relay_locks"))
        transaction.execute(DatabaseStatement("DELETE FROM af_outbox"))
        outbox.append(transaction, _event(backend, "accepted"))
        outbox.append(transaction, retry_event)

    with relay.lock() as lease:
        lease.renew()
        with pytest.raises(OutboxRelayLeaseUnavailable), competitor.lock():
            pass

    first_report = relay.run_once()
    assert first_report.selected == 2
    assert first_report.published == 1
    assert first_report.retried == 1
    assert first_report.dead_lettered == 0
    assert publisher.message_ids == [
        f"relay-{backend}-accepted",
        f"relay-{backend}-retry",
    ]

    clock.advance(timedelta(seconds=1))
    second_report = relay.run_once()
    assert second_report.selected == 1
    assert second_report.published == 1
    assert second_report.retried == 0
    assert second_report.dead_lettered == 0
    assert publisher.message_ids[-1] == f"relay-{backend}-retry"
    with first.transaction(read_only=True) as transaction:
        assert outbox.pending(transaction, now=clock()) == ()

    with first.transaction() as transaction:
        transaction.execute(DatabaseStatement("DROP TABLE af_outbox_relay_locks"))
        transaction.execute(DatabaseStatement("DROP TABLE af_outbox"))


@pytest.mark.parametrize(
    ("backend", "environment", "factory"),
    [
        ("postgresql", "AF_POSTGRESQL_TEST_URL", PostgreSQLDriver),
        ("mysql", "AF_MYSQL_TEST_URL", MySQLDriver),
        ("mariadb", "AF_MARIADB_TEST_URL", MariaDBDriver),
    ],
)
def test_outbox_relay_passes_real_relational_services(
    backend: str,
    environment: str,
    factory: type[PostgreSQLDriver] | type[MySQLDriver] | type[MariaDBDriver],
) -> None:
    url = os.environ.get(environment)
    if url is None:
        pytest.skip(f"{environment} is not configured")
    with factory(url) as first, factory(url) as second:
        _exercise(first, second, backend)
