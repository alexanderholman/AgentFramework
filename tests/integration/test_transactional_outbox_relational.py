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
    TransactionalOutbox,
)

_NOW = datetime(2026, 7, 24, 8, tzinfo=UTC)


def _event(backend: str, suffix: str) -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=f"outbox-{backend}-{suffix}",
            message_type="outbox.conformance",
            schema_version=1,
            kind=EnvelopeKind.INTEGRATION_EVENT,
            occurred_at=_NOW,
        ),
        payload={"backend": backend, "suffix": suffix},
    )


def _exercise(driver: DatabaseDriver, backend: str) -> None:
    outbox = TransactionalOutbox()
    committed = _event(backend, "committed")
    rolled_back = _event(backend, "rolled-back")

    with driver.transaction() as transaction:
        outbox.install(transaction)
        transaction.execute(
            DatabaseStatement(
                "CREATE TABLE IF NOT EXISTS af_outbox_conformance (value VARCHAR(128))"
            )
        )
        transaction.execute(DatabaseStatement("DELETE FROM af_outbox"))
        transaction.execute(DatabaseStatement("DELETE FROM af_outbox_conformance"))

    with driver.transaction() as transaction:
        transaction.execute(
            DatabaseStatement(
                "INSERT INTO af_outbox_conformance (value) VALUES (:value)",
                {"value": "committed"},
            )
        )
        outbox.append(transaction, committed)

    with pytest.raises(RuntimeError, match="rollback"):
        with driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    "INSERT INTO af_outbox_conformance (value) VALUES (:value)",
                    {"value": "rolled-back"},
                )
            )
            outbox.append(transaction, rolled_back)
            raise RuntimeError("rollback")

    with driver.transaction(read_only=True) as transaction:
        values = transaction.query(
            DatabaseStatement(
                "SELECT value FROM af_outbox_conformance ORDER BY value",
                read_only=True,
            )
        )
        pending = outbox.pending(transaction, now=_NOW)

    assert tuple(row["value"] for row in values) == ("committed",)
    assert tuple(record.envelope for record in pending) == (committed,)

    retry_at = _NOW + timedelta(seconds=1)
    with driver.transaction() as transaction:
        assert outbox.mark_retry(
            transaction,
            committed.metadata.message_id,
            available_at=retry_at,
            error_code="transport.failed",
        )

    with driver.transaction(read_only=True) as transaction:
        assert outbox.pending(transaction, now=_NOW) == ()
        retried = outbox.pending(transaction, now=retry_at)

    assert len(retried) == 1
    assert retried[0].attempt_count == 1
    assert retried[0].last_error_code == "transport.failed"

    with driver.transaction() as transaction:
        assert outbox.mark_published(
            transaction,
            committed.metadata.message_id,
            published_at=retry_at,
        )

    with driver.transaction(read_only=True) as transaction:
        assert outbox.pending(transaction, now=retry_at) == ()

    with driver.transaction() as transaction:
        transaction.execute(DatabaseStatement("DROP TABLE af_outbox_conformance"))
        transaction.execute(DatabaseStatement("DROP TABLE af_outbox"))


@pytest.mark.parametrize(
    ("backend", "environment", "factory"),
    [
        ("postgresql", "AF_POSTGRESQL_TEST_URL", PostgreSQLDriver),
        ("mysql", "AF_MYSQL_TEST_URL", MySQLDriver),
        ("mariadb", "AF_MARIADB_TEST_URL", MariaDBDriver),
    ],
)
def test_transactional_outbox_passes_real_relational_services(
    backend: str,
    environment: str,
    factory: type[PostgreSQLDriver] | type[MySQLDriver] | type[MariaDBDriver],
) -> None:
    url = os.environ.get(environment)
    if url is None:
        pytest.skip(f"{environment} is not configured")
    with factory(url) as driver:
        _exercise(driver, backend)
