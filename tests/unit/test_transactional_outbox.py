from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from af.framework.relational import DatabaseOperationError, DatabaseStatement
from af.framework.unit_of_work import UnitOfWork
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import (
    EnvelopeKind,
    MessageEnvelope,
    MessageMetadata,
    OutboxIntegrityError,
    TransactionalOutbox,
)

_NOW = datetime(2026, 7, 24, 8, tzinfo=UTC)


def _event(
    message_id: str,
    *,
    occurred_at: datetime = _NOW,
    kind: EnvelopeKind = EnvelopeKind.INTEGRATION_EVENT,
) -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=message_id,
            message_type="records.changed",
            schema_version=1,
            kind=kind,
            occurred_at=occurred_at,
        ),
        payload={"message_id": message_id, "secret": "not-for-repr"},
    )


def _setup() -> tuple[SQLiteDriver, TransactionalOutbox]:
    driver = SQLiteDriver(":memory:")
    outbox = TransactionalOutbox()
    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("CREATE TABLE records (value TEXT)"))
        outbox.install(transaction)
        outbox.install(transaction)
    return driver, outbox


def _record_count(driver: SQLiteDriver) -> int:
    with driver.transaction(read_only=True) as transaction:
        value = transaction.query(
            DatabaseStatement("SELECT COUNT(*) AS count FROM records", read_only=True)
        )[0]["count"]
    assert type(value) is int
    return value


def test_authoritative_write_and_outbox_append_commit_atomically() -> None:
    driver, outbox = _setup()
    event = _event("event-commit")

    with UnitOfWork(driver) as unit:
        unit.transaction.execute(
            DatabaseStatement(
                "INSERT INTO records VALUES (:value)",
                {"value": "committed"},
            )
        )
        outbox.append(unit.transaction, event)

    with driver.transaction(read_only=True) as transaction:
        pending = outbox.pending(transaction, now=_NOW)

    assert _record_count(driver) == 1
    assert len(pending) == 1
    assert pending[0].envelope == event
    assert pending[0].attempt_count == 0
    assert pending[0].available_at == _NOW
    assert pending[0].last_error_code is None
    assert "not-for-repr" not in repr(pending[0])
    driver.close()


def test_rollback_prevents_both_state_change_and_outbox_publication() -> None:
    driver, outbox = _setup()

    with pytest.raises(RuntimeError, match="rollback"), UnitOfWork(driver) as unit:
        unit.transaction.execute(
            DatabaseStatement(
                "INSERT INTO records VALUES (:value)",
                {"value": "discarded"},
            )
        )
        outbox.append(unit.transaction, _event("event-rollback"))
        raise RuntimeError("rollback")

    with driver.transaction(read_only=True) as transaction:
        pending = outbox.pending(transaction, now=_NOW)

    assert _record_count(driver) == 0
    assert pending == ()
    driver.close()


def test_duplicate_message_id_fails_without_creating_a_second_record() -> None:
    driver, outbox = _setup()
    event = _event("event-duplicate")

    with driver.transaction() as transaction:
        outbox.append(transaction, event)

    with pytest.raises(DatabaseOperationError), driver.transaction() as transaction:
        outbox.append(transaction, event)

    with driver.transaction(read_only=True) as transaction:
        pending = outbox.pending(transaction, now=_NOW)

    assert tuple(record.envelope.metadata.message_id for record in pending) == (
        "event-duplicate",
    )
    driver.close()


def test_pending_records_are_due_only_and_deterministically_ordered() -> None:
    driver, outbox = _setup()
    first_available = _NOW + timedelta(seconds=1)
    later_available = _NOW + timedelta(seconds=2)

    with driver.transaction() as transaction:
        outbox.append(
            transaction,
            _event("event-b", occurred_at=_NOW),
            available_at=first_available,
        )
        outbox.append(
            transaction,
            _event("event-a", occurred_at=_NOW),
            available_at=first_available,
        )
        outbox.append(
            transaction,
            _event("event-future", occurred_at=_NOW - timedelta(minutes=1)),
            available_at=later_available,
        )

    with driver.transaction(read_only=True) as transaction:
        pending = outbox.pending(transaction, now=first_available)

    assert tuple(record.envelope.metadata.message_id for record in pending) == (
        "event-a",
        "event-b",
    )
    with pytest.raises(ValueError, match="pending limit"):
        with driver.transaction(read_only=True) as transaction:
            outbox.pending(transaction, now=_NOW, limit=0)
    driver.close()


def test_retry_publish_and_dead_letter_transitions_control_pending_state() -> None:
    driver, outbox = _setup()
    retry_at = _NOW + timedelta(minutes=1)

    with driver.transaction() as transaction:
        outbox.append(transaction, _event("event-retry"))
        outbox.append(transaction, _event("event-publish"))
        outbox.append(transaction, _event("event-dead"))

    with driver.transaction() as transaction:
        assert outbox.mark_retry(
            transaction,
            "event-retry",
            available_at=retry_at,
            error_code="transport.failed",
        )
        assert outbox.mark_published(
            transaction,
            "event-publish",
            published_at=_NOW,
        )
        assert outbox.mark_dead_letter(
            transaction,
            "event-dead",
            dead_lettered_at=_NOW,
            error_code="delivery.exhausted",
        )

    with driver.transaction(read_only=True) as transaction:
        assert outbox.pending(transaction, now=_NOW) == ()
        pending = outbox.pending(transaction, now=retry_at)

    assert len(pending) == 1
    assert pending[0].envelope.metadata.message_id == "event-retry"
    assert pending[0].attempt_count == 1
    assert pending[0].last_error_code == "transport.failed"

    with driver.transaction() as transaction:
        assert not outbox.mark_published(
            transaction,
            "event-publish",
            published_at=_NOW + timedelta(seconds=1),
        )
        assert not outbox.mark_dead_letter(
            transaction,
            "event-dead",
            dead_lettered_at=_NOW + timedelta(seconds=1),
            error_code="delivery.exhausted",
        )
        assert not outbox.mark_retry(
            transaction,
            "missing",
            available_at=retry_at,
            error_code="transport.failed",
        )
    driver.close()


def test_outbox_rejects_non_integration_events_before_database_mutation() -> None:
    driver, outbox = _setup()

    with pytest.raises(ValueError, match="integration events only"):
        with driver.transaction() as transaction:
            outbox.append(
                transaction,
                _event("event-domain", kind=EnvelopeKind.DOMAIN_EVENT),
            )

    with driver.transaction(read_only=True) as transaction:
        assert outbox.pending(transaction, now=_NOW) == ()
    driver.close()


def test_corrupt_stored_envelope_fails_closed_without_exposing_payload() -> None:
    driver, outbox = _setup()

    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                """INSERT INTO af_outbox (
                    message_id,
                    message_type,
                    schema_version,
                    occurred_at,
                    available_at,
                    envelope_text,
                    attempt_count,
                    published_at,
                    dead_lettered_at,
                    last_error_code
                ) VALUES (
                    :message_id,
                    :message_type,
                    :schema_version,
                    :occurred_at,
                    :available_at,
                    :envelope_text,
                    0,
                    NULL,
                    NULL,
                    NULL
                )""",
                {
                    "message_id": "event-corrupt",
                    "message_type": "records.changed",
                    "schema_version": 1,
                    "occurred_at": "2026-07-24T08:00:00.000000Z",
                    "available_at": "2026-07-24T08:00:00.000000Z",
                    "envelope_text": "secret-invalid-envelope",
                },
            )
        )

    with pytest.raises(OutboxIntegrityError) as captured:
        with driver.transaction(read_only=True) as transaction:
            outbox.pending(transaction, now=_NOW)

    assert captured.value.reason is not None
    assert "secret-invalid-envelope" not in str(captured.value)
    assert "secret-invalid-envelope" not in repr(captured.value)
    driver.close()


@pytest.mark.parametrize(
    ("message_id", "error_code"),
    [
        ("", "transport.failed"),
        ("event-1", "INVALID CODE"),
    ],
)
def test_outbox_transitions_reject_invalid_identifiers(
    message_id: str,
    error_code: str,
) -> None:
    driver, outbox = _setup()

    with pytest.raises(ValueError):
        with driver.transaction() as transaction:
            outbox.mark_retry(
                transaction,
                message_id,
                available_at=_NOW,
                error_code=error_code,
            )
    driver.close()
