"""Transactional relational outbox storage for canonical integration events."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    DatabaseTransaction,
)
from af.messaging.envelopes import (
    EnvelopeKind,
    JsonEnvelopeCodec,
    MessageEnvelope,
)

__all__ = (
    "OutboxIntegrityError",
    "OutboxRecord",
    "TransactionalOutbox",
)

_TABLE = "af_outbox"
_MAX_PENDING_LIMIT = 1_000
_MAX_ENVELOPE_BYTES = 1_048_576
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")


class OutboxIntegrityError(DatabaseOperationError):
    """Stored outbox state cannot be reconciled with its canonical envelope."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            reason=PlatformReason.INTEGRITY_FAILURE,
            stage="outbox.integrity",
            protected_cause=protected_cause,
        )


@dataclass(frozen=True, slots=True, repr=False)
class OutboxRecord:
    """One pending canonical envelope and bounded delivery state."""

    envelope: MessageEnvelope
    attempt_count: int
    available_at: datetime
    last_error_code: str | None

    def __repr__(self) -> str:
        return (
            "OutboxRecord(envelope='[REDACTED]', "
            f"attempt_count={self.attempt_count!r}, "
            f"available_at={self.available_at.isoformat()!r}, "
            f"last_error_code={self.last_error_code!r})"
        )


class TransactionalOutbox:
    """Persist integration events inside an existing authoritative transaction.

    This component is storage only. It does not claim durable relay, leasing, acknowledgement,
    exactly-once delivery, inbox deduplication, or projection checkpoint semantics.
    """

    def __init__(self, *, codec: JsonEnvelopeCodec | None = None) -> None:
        resolved = JsonEnvelopeCodec() if codec is None else codec
        if not isinstance(resolved, JsonEnvelopeCodec):
            raise TypeError("outbox codec must be a JsonEnvelopeCodec")
        self._codec = resolved

    def install(self, transaction: DatabaseTransaction, /) -> None:
        """Create the fixed outbox table inside the caller-owned transaction."""

        transaction.execute(
            DatabaseStatement(
                f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                    message_id VARCHAR(200) PRIMARY KEY,
                    message_type VARCHAR(128) NOT NULL,
                    schema_version INTEGER NOT NULL,
                    occurred_at VARCHAR(32) NOT NULL,
                    available_at VARCHAR(32) NOT NULL,
                    envelope_text TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    published_at VARCHAR(32) NULL,
                    dead_lettered_at VARCHAR(32) NULL,
                    last_error_code VARCHAR(128) NULL
                )"""
            )
        )

    def append(
        self,
        transaction: DatabaseTransaction,
        envelope: MessageEnvelope,
        /,
        *,
        available_at: datetime | None = None,
    ) -> None:
        """Atomically append one canonical integration event to the caller transaction."""

        if not isinstance(envelope, MessageEnvelope):
            raise TypeError("outbox append requires a MessageEnvelope")
        if envelope.metadata.kind is not EnvelopeKind.INTEGRATION_EVENT:
            raise ValueError("outbox accepts integration events only")
        available = envelope.metadata.occurred_at if available_at is None else _aware_utc(
            available_at,
            "outbox availability",
        )
        encoded = self._codec.encode(envelope)
        if len(encoded) > _MAX_ENVELOPE_BYTES:
            raise ValueError("outbox envelope exceeds relational storage bounds")
        text = encoded.decode("utf-8", errors="strict")
        if len(text) > _MAX_ENVELOPE_BYTES:
            raise ValueError("outbox envelope exceeds relational storage bounds")

        transaction.execute(
            DatabaseStatement(
                f"""INSERT INTO {_TABLE} (
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
                    "message_id": envelope.metadata.message_id,
                    "message_type": envelope.metadata.message_type,
                    "schema_version": envelope.metadata.schema_version,
                    "occurred_at": _timestamp(envelope.metadata.occurred_at),
                    "available_at": _timestamp(available),
                    "envelope_text": text,
                },
            )
        )

    def pending(
        self,
        transaction: DatabaseTransaction,
        /,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[OutboxRecord, ...]:
        """Return due unpublished records in deterministic relay order."""

        current = _aware_utc(now, "outbox pending time")
        if type(limit) is not int or not 1 <= limit <= _MAX_PENDING_LIMIT:
            raise ValueError("outbox pending limit is invalid")
        rows = transaction.query(
            DatabaseStatement(
                f"""SELECT
                    message_id,
                    message_type,
                    schema_version,
                    occurred_at,
                    available_at,
                    envelope_text,
                    attempt_count,
                    last_error_code
                FROM {_TABLE}
                WHERE published_at IS NULL
                  AND dead_lettered_at IS NULL
                  AND available_at <= :now
                ORDER BY available_at ASC, occurred_at ASC, message_id ASC
                LIMIT :limit""",
                {"now": _timestamp(current), "limit": limit},
                read_only=True,
            )
        )
        return tuple(self._record(row) for row in rows)

    def mark_published(
        self,
        transaction: DatabaseTransaction,
        message_id: str,
        /,
        *,
        published_at: datetime,
    ) -> bool:
        """Transition one live record to published; repeated calls are state-idempotent."""

        identifier = _message_id(message_id)
        changed = transaction.execute(
            DatabaseStatement(
                f"""UPDATE {_TABLE}
                SET published_at = :published_at
                WHERE message_id = :message_id
                  AND published_at IS NULL
                  AND dead_lettered_at IS NULL""",
                {
                    "message_id": identifier,
                    "published_at": _timestamp(
                        _aware_utc(published_at, "outbox publication time")
                    ),
                },
            )
        )
        return _transition_count(changed)

    def mark_retry(
        self,
        transaction: DatabaseTransaction,
        message_id: str,
        /,
        *,
        available_at: datetime,
        error_code: str,
    ) -> bool:
        """Record one failed relay attempt and schedule the next eligible attempt."""

        identifier = _message_id(message_id)
        code = _error_code(error_code)
        changed = transaction.execute(
            DatabaseStatement(
                f"""UPDATE {_TABLE}
                SET attempt_count = attempt_count + 1,
                    available_at = :available_at,
                    last_error_code = :error_code
                WHERE message_id = :message_id
                  AND published_at IS NULL
                  AND dead_lettered_at IS NULL""",
                {
                    "message_id": identifier,
                    "available_at": _timestamp(
                        _aware_utc(available_at, "outbox retry time")
                    ),
                    "error_code": code,
                },
            )
        )
        return _transition_count(changed)

    def mark_dead_letter(
        self,
        transaction: DatabaseTransaction,
        message_id: str,
        /,
        *,
        dead_lettered_at: datetime,
        error_code: str,
    ) -> bool:
        """Move one live record out of the pending set after a terminal relay failure."""

        identifier = _message_id(message_id)
        code = _error_code(error_code)
        changed = transaction.execute(
            DatabaseStatement(
                f"""UPDATE {_TABLE}
                SET attempt_count = attempt_count + 1,
                    dead_lettered_at = :dead_lettered_at,
                    last_error_code = :error_code
                WHERE message_id = :message_id
                  AND published_at IS NULL
                  AND dead_lettered_at IS NULL""",
                {
                    "message_id": identifier,
                    "dead_lettered_at": _timestamp(
                        _aware_utc(dead_lettered_at, "outbox dead-letter time")
                    ),
                    "error_code": code,
                },
            )
        )
        return _transition_count(changed)

    def _record(self, row: DatabaseRow) -> OutboxRecord:
        try:
            message_id = _stored_text(row["message_id"])
            message_type = _stored_text(row["message_type"])
            schema_version = _stored_int(row["schema_version"])
            occurred_at = _parse_timestamp(_stored_text(row["occurred_at"]))
            available_at = _parse_timestamp(_stored_text(row["available_at"]))
            envelope_text = _stored_text(row["envelope_text"])
            attempt_count = _stored_int(row["attempt_count"])
            last_error_value = row["last_error_code"]
            last_error_code = (
                None if last_error_value is None else _error_code(_stored_text(last_error_value))
            )
            envelope = self._codec.decode(envelope_text.encode("utf-8"))
            if (
                envelope.metadata.kind is not EnvelopeKind.INTEGRATION_EVENT
                or envelope.metadata.message_id != message_id
                or envelope.metadata.message_type != message_type
                or envelope.metadata.schema_version != schema_version
                or envelope.metadata.occurred_at != occurred_at
                or attempt_count < 0
            ):
                raise ValueError("outbox row and canonical envelope diverge")
        except (KeyError, TypeError, ValueError, UnicodeError) as error:
            raise OutboxIntegrityError(protected_cause=error) from None
        return OutboxRecord(
            envelope=envelope,
            attempt_count=attempt_count,
            available_at=available_at,
            last_error_code=last_error_code,
        )


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("outbox timestamp is not canonical UTC")
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    normalized = _aware_utc(parsed, "outbox stored time")
    if _timestamp(normalized) != value:
        raise ValueError("outbox timestamp is not canonical UTC")
    return normalized


def _message_id(value: str) -> str:
    if type(value) is not str or not value or len(value) > 200 or re.search(
        r"[\x00-\x1f\x7f-\x9f\u2028\u2029]",
        value,
    ):
        raise ValueError("outbox message id is invalid")
    return value


def _error_code(value: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("outbox error code must be a canonical identifier")
    return value


def _stored_text(value: object) -> str:
    if type(value) is not str:
        raise TypeError("outbox stored text is invalid")
    return value


def _stored_int(value: object) -> int:
    if type(value) is not int:
        raise TypeError("outbox stored integer is invalid")
    return value


def _transition_count(changed: int) -> bool:
    if type(changed) is not int or changed not in (0, 1):
        raise OutboxIntegrityError()
    return changed == 1
