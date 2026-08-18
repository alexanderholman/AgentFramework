"""Durable relational queue driver with bounded retry and dead-letter semantics."""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from af.framework.errors import PlatformReason
from af.framework.relational import DatabaseDriver, DatabaseOperationError, DatabaseStatement
from af.messaging.envelopes import JsonEnvelopeCodec, MessageEnvelope
from af.queue.contracts import (
    QueueDeadLetter,
    QueueDeadLetterReplayReceipt,
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueJobTags,
    QueueOperationError,
    QueueReservation,
    QueueReservationLost,
    UnsupportedQueueFeature,
)
from af.queue.manager import QueueFeature

__all__ = ("DatabaseQueueDriver",)

_FEATURES = frozenset(
    {
        QueueFeature.DEAD_LETTER_INSPECTION,
        QueueFeature.DEAD_LETTER_REPLAY,
        QueueFeature.RESERVATION,
        QueueFeature.RESERVATION_RENEWAL,
        QueueFeature.RETRY,
    }
)
_TABLE = "af_queue_jobs"
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_MAX_ENVELOPE_BYTES = 1_048_576
_DEFAULT_RESERVATION_LEASE = timedelta(minutes=5)
_MAX_RESERVATION_LEASE = timedelta(days=1)
_MAX_RESERVATION_GENERATION = 2_147_483_647
_OWNERSHIP_TOKEN = re.compile(r"^[0-9a-f]{64}$")


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"queue {field} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, field: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"queue stored {field} is invalid")
    return _aware_utc(datetime.fromisoformat(f"{value[:-1]}+00:00"), field)


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"queue {field} must be a canonical identifier")
    return value


def _unsupported_feature(job: QueueJob) -> QueueFeature | None:
    if job.delay is not None:
        return QueueFeature.DELAY
    if job.priority is not None:
        return QueueFeature.PRIORITY
    if job.timeout is not None:
        return QueueFeature.TIMEOUT
    if job.unique_key is not None:
        return QueueFeature.UNIQUENESS
    if job.fifo_group is not None:
        return QueueFeature.FIFO
    if job.requires_transaction:
        return QueueFeature.TRANSACTIONAL_DISPATCH
    return None


def _attempt_conflict() -> QueueOperationError:
    return QueueOperationError(
        reason=PlatformReason.INTEGRITY_FAILURE,
        stage="queue.reservation",
    )


def _is_database_driver(value: object) -> bool:
    return all(callable(getattr(value, name, None)) for name in ("transaction", "close"))


class DatabaseQueueDriver:
    """Persist jobs in one caller-provided relational driver."""

    def __init__(
        self,
        database: DatabaseDriver,
        *,
        clock: Callable[[], datetime],
        codec: JsonEnvelopeCodec | None = None,
        reservation_lease: timedelta = _DEFAULT_RESERVATION_LEASE,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        if not _is_database_driver(database):
            raise TypeError("database queue requires a DatabaseDriver")
        if not callable(clock):
            raise TypeError("database queue clock must be callable")
        if token_factory is not None and not callable(token_factory):
            raise TypeError("database queue token factory must be callable")
        if (
            not isinstance(reservation_lease, timedelta)
            or reservation_lease <= timedelta(0)
            or reservation_lease > _MAX_RESERVATION_LEASE
        ):
            raise ValueError("database queue reservation lease must be within one day")
        resolved_codec = JsonEnvelopeCodec() if codec is None else codec
        if not isinstance(resolved_codec, JsonEnvelopeCodec):
            raise TypeError("database queue codec must be a JsonEnvelopeCodec")
        self._database = database
        self._clock = clock
        self._codec = resolved_codec
        self._reservation_lease = reservation_lease
        self._token_factory = token_factory or (lambda: secrets.token_hex(32))
        self._process_id = os.getpid()
        self._reservation_owner_id = self._new_token(stage="queue.connection")
        self._closed = False
        self._install()

    @property
    def features(self) -> frozenset[QueueFeature]:
        return _FEATURES

    def supports(self, feature: QueueFeature, /) -> bool:
        if not isinstance(feature, QueueFeature):
            raise TypeError("queue feature is invalid")
        return feature in _FEATURES

    def require(self, feature: QueueFeature, /) -> None:
        if not self.supports(feature):
            raise UnsupportedQueueFeature(feature.value)

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt:
        self._assert_open()
        if not isinstance(job, QueueJob):
            raise TypeError("database queue dispatch requires a QueueJob")
        unsupported = _unsupported_feature(job)
        if unsupported is not None:
            raise UnsupportedQueueFeature(unsupported.value)
        dispatched_at = _aware_utc(self._clock(), "dispatch time")
        encoded = self._codec.encode(job.envelope)
        if len(encoded) > _MAX_ENVELOPE_BYTES:
            raise ValueError("queue envelope exceeds relational storage bounds")
        envelope_text = encoded.decode("utf-8", errors="strict")
        available_at = job.envelope.metadata.occurred_at
        try:
            with self._database.transaction() as transaction:
                transaction.execute(
                    DatabaseStatement(
                        f"""INSERT INTO {_TABLE} (
                            message_id,
                            message_type,
                            schema_version,
                            occurred_at,
                            lane,
                            available_at,
                            envelope_text,
                            tags_text,
                            attempt_count,
                            reservation_generation,
                            max_attempts,
                            backoff_seconds,
                            reserved_at,
                            reservation_expires_at,
                            reservation_owner_id,
                            reservation_id,
                            dead_lettered_at
                        ) VALUES (
                            :message_id,
                            :message_type,
                            :schema_version,
                            :occurred_at,
                            :lane,
                            :available_at,
                            :envelope_text,
                            :tags_text,
                            0,
                            0,
                            :max_attempts,
                            :backoff_seconds,
                            NULL,
                            NULL,
                            NULL,
                            NULL,
                            NULL
                        )""",
                        {
                            "message_id": job.envelope.metadata.message_id,
                            "message_type": job.envelope.metadata.message_type,
                            "schema_version": job.envelope.metadata.schema_version,
                            "occurred_at": _timestamp(job.envelope.metadata.occurred_at),
                            "lane": job.lane,
                            "available_at": _timestamp(available_at),
                            "envelope_text": envelope_text,
                            "tags_text": self._encode_tags(job.tags),
                            "max_attempts": job.max_attempts,
                            "backoff_seconds": (
                                None if job.backoff is None else job.backoff.total_seconds()
                            ),
                        },
                    )
                )
        except DatabaseOperationError as error:
            raise QueueOperationError(
                reason=error.reason,
                stage="queue.dispatch",
                protected_cause=error,
            ) from None
        return QueueDispatchReceipt(
            message_id=job.envelope.metadata.message_id,
            lane=job.lane,
            disposition=QueueDispatchDisposition.ENQUEUED,
            dispatched_at=dispatched_at,
        )

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        self._assert_open()
        current = _aware_utc(self._clock(), "reservation time")
        expires_at = current + self._reservation_lease
        legacy_cutoff = current - self._reservation_lease
        if lane is not None:
            _identifier(lane, "lane")
        try:
            with self._database.transaction() as transaction:
                eligibility = """(
                    reserved_at IS NULL
                    OR reservation_expires_at <= :now
                    OR (reservation_expires_at IS NULL AND reserved_at <= :legacy_cutoff)
                )"""
                where = f"available_at <= :now AND {eligibility} AND dead_lettered_at IS NULL"
                parameters: dict[str, object] = {
                    "now": _timestamp(current),
                    "legacy_cutoff": _timestamp(legacy_cutoff),
                }
                if lane is not None:
                    where += " AND lane = :lane"
                    parameters["lane"] = lane
                rows = transaction.query(
                    DatabaseStatement(
                        f"""SELECT
                            message_id,
                            occurred_at,
                            lane,
                            envelope_text,
                            tags_text,
                            attempt_count,
                            reservation_generation,
                            reserved_at,
                            reservation_expires_at,
                            max_attempts,
                            backoff_seconds
                        FROM {_TABLE}
                        WHERE {where}
                        ORDER BY available_at ASC, occurred_at ASC, message_id ASC
                        LIMIT 1""",
                        parameters,
                        read_only=True,
                    )
                )
                if not rows:
                    return None
                row = rows[0]
                message_id = self._stored_text(row["message_id"])
                previous_attempts = self._stored_int(row["attempt_count"])
                recovering = row["reserved_at"] is not None
                consumed_attempts = previous_attempts + (1 if recovering else 0)
                previous_generation = self._stored_int(row["reservation_generation"])
                claim_parameters = {
                    "reserved_at": _timestamp(current),
                    "reservation_expires_at": _timestamp(expires_at),
                    "message_id": message_id,
                    "previous_generation": previous_generation,
                    "attempt_count": consumed_attempts,
                    "now": _timestamp(current),
                    "legacy_cutoff": _timestamp(legacy_cutoff),
                }
                if recovering and consumed_attempts >= self._stored_int(row["max_attempts"]):
                    changed = transaction.execute(
                        DatabaseStatement(
                            f"""UPDATE {_TABLE}
                            SET attempt_count = :attempt_count,
                                reserved_at = NULL,
                                reservation_expires_at = NULL,
                                reservation_owner_id = NULL,
                                reservation_id = NULL,
                                dead_lettered_at = :now
                            WHERE message_id = :message_id
                              AND reservation_generation = :previous_generation
                              AND {eligibility}
                              AND dead_lettered_at IS NULL""",
                            claim_parameters,
                        )
                    )
                    if changed == 0:
                        return None
                    if changed != 1:
                        raise _attempt_conflict()
                    return None
                if previous_generation >= _MAX_RESERVATION_GENERATION:
                    raise _attempt_conflict()
                generation = previous_generation + 1
                reservation_id = self._new_token(stage="queue.reserve")
                claim_parameters.update(
                    {
                        "reservation_generation": generation,
                        "reservation_owner_id": self._reservation_owner_id,
                        "reservation_id": reservation_id,
                    }
                )
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_TABLE}
                        SET reservation_generation = :reservation_generation,
                            attempt_count = :attempt_count,
                            reserved_at = :reserved_at,
                            reservation_expires_at = :reservation_expires_at,
                            reservation_owner_id = :reservation_owner_id,
                            reservation_id = :reservation_id
                        WHERE message_id = :message_id
                          AND reservation_generation = :previous_generation
                          AND {eligibility}
                          AND dead_lettered_at IS NULL""",
                        claim_parameters,
                    )
                )
                if changed == 0:
                    return None
                if changed != 1:
                    raise _attempt_conflict()
                return QueueReservation(
                    job=self._job_from_row(row),
                    attempt=consumed_attempts + 1,
                    reserved_at=current,
                    fencing_token=generation,
                    expires_at=expires_at,
                    owner_id=self._reservation_owner_id,
                    reservation_id=reservation_id,
                )
        except QueueOperationError:
            raise
        except DatabaseOperationError as error:
            raise QueueOperationError(
                reason=error.reason,
                stage="queue.reserve",
                protected_cause=error,
            ) from None

    def ack(self, reservation: QueueReservation, /) -> None:
        self._assert_open()
        self._transition_reserved(
            reservation,
            stage="queue.ack",
            sql=(
                f"DELETE FROM {_TABLE} WHERE message_id = :message_id "
                f"AND attempt_count = :previous_attempt "
                f"AND reservation_generation = :fencing_token "
                f"AND reservation_owner_id = :reservation_owner_id "
                f"AND reservation_id = :reservation_id "
                f"AND reserved_at = :reserved_at "
                f"AND reservation_expires_at = :reservation_expires_at "
                f"AND reservation_expires_at > :now "
                f"AND dead_lettered_at IS NULL"
            ),
        )

    def reject(self, reservation: QueueReservation, /) -> None:
        self._assert_open()
        self._transition_reserved(
            reservation,
            stage="queue.reject",
            sql=(
                f"UPDATE {_TABLE} SET reserved_at = NULL, reservation_expires_at = NULL, "
                f"reservation_owner_id = NULL, reservation_id = NULL, "
                f"attempt_count = :attempt, dead_lettered_at = :terminal_at "
                f"WHERE message_id = :message_id AND attempt_count = :previous_attempt "
                f"AND reservation_generation = :fencing_token "
                f"AND reservation_owner_id = :reservation_owner_id "
                f"AND reservation_id = :reservation_id "
                f"AND reserved_at = :reserved_at "
                f"AND reservation_expires_at = :reservation_expires_at "
                f"AND reservation_expires_at > :now AND dead_lettered_at IS NULL"
            ),
            terminal_at=_aware_utc(self._clock(), "dead-letter time"),
        )

    def release(self, reservation: QueueReservation, /) -> None:
        """Release interrupted work without exhausting its delivery budget."""
        self._assert_open()
        self._transition_reserved(
            reservation,
            stage="queue.release",
            sql=(
                f"UPDATE {_TABLE} SET reserved_at = NULL, reservation_expires_at = NULL, "
                f"reservation_owner_id = NULL, reservation_id = NULL, "
                f"available_at = :available_at, "
                f"attempt_count = :previous_attempt WHERE message_id = :message_id "
                f"AND attempt_count = :previous_attempt "
                f"AND reservation_generation = :fencing_token "
                f"AND reservation_owner_id = :reservation_owner_id "
                f"AND reservation_id = :reservation_id "
                f"AND reserved_at = :reserved_at "
                f"AND reservation_expires_at = :reservation_expires_at "
                f"AND reservation_expires_at > :now "
                f"AND dead_lettered_at IS NULL"
            ),
            available_at=_aware_utc(self._clock(), "release time"),
        )

    def renew(self, reservation: QueueReservation, /) -> QueueReservation:
        """Extend one live owned lease and rotate its acquisition capability."""

        self._assert_open()
        if not isinstance(reservation, QueueReservation):
            raise TypeError("database queue renew requires a QueueReservation")
        if reservation.fencing_token is None:
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="queue.renew",
            )
        if reservation.expires_at is None:
            raise QueueReservationLost(stage="queue.renew")
        if (
            reservation.owner_id is None
            or reservation.reservation_id is None
            or not secrets.compare_digest(reservation.owner_id, self._reservation_owner_id)
        ):
            raise QueueReservationLost(stage="queue.renew")

        try:
            with self._database.transaction() as transaction:
                current = _aware_utc(self._clock(), "renewal time")
                renewed_expiry = current + self._reservation_lease
                if renewed_expiry <= reservation.expires_at:
                    raise QueueOperationError(
                        reason=PlatformReason.INTEGRITY_FAILURE,
                        stage="queue.renew",
                    )
                renewed_id = self._new_token(stage="queue.renew")
                if secrets.compare_digest(renewed_id, reservation.reservation_id):
                    raise QueueOperationError(
                        reason=PlatformReason.INTEGRITY_FAILURE,
                        stage="queue.renew",
                    )
                parameters: dict[str, object] = {
                    "message_id": reservation.job.envelope.metadata.message_id,
                    "previous_attempt": reservation.attempt - 1,
                    "fencing_token": reservation.fencing_token,
                    "reserved_at": _timestamp(reservation.reserved_at),
                    "reservation_expires_at": _timestamp(reservation.expires_at),
                    "renewed_expiry": _timestamp(renewed_expiry),
                    "now": _timestamp(current),
                    "reservation_owner_id": self._reservation_owner_id,
                    "reservation_id": reservation.reservation_id,
                    "renewed_id": renewed_id,
                }
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_TABLE}
                        SET reservation_expires_at = :renewed_expiry,
                            reservation_id = :renewed_id
                        WHERE message_id = :message_id
                          AND attempt_count = :previous_attempt
                          AND reservation_generation = :fencing_token
                          AND reservation_owner_id = :reservation_owner_id
                          AND reservation_id = :reservation_id
                          AND reserved_at = :reserved_at
                          AND reservation_expires_at = :reservation_expires_at
                          AND reservation_expires_at > :now
                          AND dead_lettered_at IS NULL""",
                        parameters,
                    )
                )
                if changed == 0:
                    raise QueueReservationLost(stage="queue.renew")
                if changed != 1:
                    raise QueueOperationError(
                        reason=PlatformReason.INTEGRITY_FAILURE,
                        stage="queue.renew",
                    )
                renewed = replace(
                    reservation,
                    expires_at=renewed_expiry,
                    reservation_id=renewed_id,
                )
        except QueueOperationError:
            raise
        except DatabaseOperationError as error:
            raise QueueOperationError(
                reason=error.reason,
                stage="queue.renew",
                protected_cause=error,
            ) from None
        return renewed

    def retry(
        self,
        reservation: QueueReservation,
        /,
        *,
        available_at: datetime | None = None,
    ) -> None:
        self._assert_open()
        if not isinstance(reservation, QueueReservation):
            raise TypeError("database queue retry requires a QueueReservation")
        if reservation.attempt >= reservation.job.max_attempts:
            self._transition_reserved(
                reservation,
                stage="queue.retry",
                sql=(
                    f"UPDATE {_TABLE} SET reserved_at = NULL, reservation_expires_at = NULL, "
                    f"reservation_owner_id = NULL, reservation_id = NULL, "
                    f"attempt_count = :attempt, dead_lettered_at = :terminal_at "
                    f"WHERE message_id = :message_id AND attempt_count = :previous_attempt "
                    f"AND reservation_generation = :fencing_token "
                    f"AND reservation_owner_id = :reservation_owner_id "
                    f"AND reservation_id = :reservation_id "
                    f"AND reserved_at = :reserved_at "
                    f"AND reservation_expires_at = :reservation_expires_at "
                    f"AND reservation_expires_at > :now AND dead_lettered_at IS NULL"
                ),
                terminal_at=_aware_utc(self._clock(), "dead-letter time"),
            )
            return
        next_available = (
            _aware_utc(available_at, "retry time")
            if available_at is not None
            else (
                reservation.reserved_at + reservation.job.backoff
                if reservation.job.backoff is not None
                else _aware_utc(self._clock(), "retry time")
            )
        )
        self._transition_reserved(
            reservation,
            stage="queue.retry",
            sql=(
                f"UPDATE {_TABLE} SET reserved_at = NULL, reservation_expires_at = NULL, "
                f"reservation_owner_id = NULL, reservation_id = NULL, "
                f"available_at = :available_at, "
                f"attempt_count = :attempt WHERE message_id = :message_id "
                f"AND attempt_count = :previous_attempt "
                f"AND reservation_generation = :fencing_token "
                f"AND reservation_owner_id = :reservation_owner_id "
                f"AND reservation_id = :reservation_id "
                f"AND reserved_at = :reserved_at "
                f"AND reservation_expires_at = :reservation_expires_at "
                f"AND reservation_expires_at > :now AND dead_lettered_at IS NULL"
            ),
            available_at=next_available,
        )

    def dead_letters(
        self,
        *,
        lane: str | None = None,
        limit: int = 100,
    ) -> tuple[QueueDeadLetter, ...]:
        """Inspect terminal deliveries in deterministic failure order."""

        self._assert_open()
        if lane is not None:
            _identifier(lane, "lane")
        if type(limit) is not int or limit < 1 or limit > 1_000:
            raise ValueError("queue dead-letter inspection limit must be within 1..1000")
        where = "dead_lettered_at IS NOT NULL"
        parameters: dict[str, object] = {"limit": limit}
        if lane is not None:
            where += " AND lane = :lane"
            parameters["lane"] = lane
        try:
            with self._database.transaction() as transaction:
                rows = transaction.query(
                    DatabaseStatement(
                        f"""SELECT
                            message_id,
                            occurred_at,
                            lane,
                            envelope_text,
                            tags_text,
                            attempt_count,
                            max_attempts,
                            backoff_seconds,
                            dead_lettered_at
                        FROM {_TABLE}
                        WHERE {where}
                        ORDER BY dead_lettered_at ASC, message_id ASC
                        LIMIT :limit""",
                        parameters,
                        read_only=True,
                    )
                )
            return tuple(
                QueueDeadLetter(
                    job=self._job_from_row(row),
                    attempts=self._stored_int(row["attempt_count"]),
                    dead_lettered_at=_parse_timestamp(
                        row["dead_lettered_at"],
                        "dead-letter time",
                    ),
                )
                for row in rows
            )
        except QueueOperationError:
            raise
        except (DatabaseOperationError, TypeError, ValueError) as error:
            reason = (
                error.reason
                if isinstance(error, DatabaseOperationError)
                else PlatformReason.INTEGRITY_FAILURE
            )
            raise QueueOperationError(
                reason=reason,
                stage="queue.dead-letter.inspect",
                protected_cause=error,
            ) from None

    def replay_dead_letter(self, message_id: str, /) -> QueueDeadLetterReplayReceipt:
        """Atomically make one terminal delivery eligible as a fresh attempt series."""

        self._assert_open()
        resolved_id = _identifier(message_id, "message id")
        replayed_at = _aware_utc(self._clock(), "replay time")
        try:
            with self._database.transaction() as transaction:
                rows = transaction.query(
                    DatabaseStatement(
                        f"""SELECT attempt_count FROM {_TABLE}
                        WHERE message_id = :message_id AND dead_lettered_at IS NOT NULL""",
                        {"message_id": resolved_id},
                        read_only=True,
                    )
                )
                if len(rows) != 1:
                    raise QueueOperationError(
                        reason=PlatformReason.INTEGRITY_FAILURE,
                        stage="queue.dead-letter.replay",
                    )
                previous_attempts = self._stored_int(rows[0]["attempt_count"])
                if previous_attempts < 1:
                    raise QueueOperationError(
                        reason=PlatformReason.INTEGRITY_FAILURE,
                        stage="queue.dead-letter.replay",
                    )
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_TABLE}
                        SET available_at = :available_at,
                            attempt_count = 0,
                            reserved_at = NULL,
                            reservation_expires_at = NULL,
                            reservation_owner_id = NULL,
                            reservation_id = NULL,
                            dead_lettered_at = NULL
                        WHERE message_id = :message_id AND dead_lettered_at IS NOT NULL""",
                        {
                            "message_id": resolved_id,
                            "available_at": _timestamp(replayed_at),
                        },
                    )
                )
                if changed != 1:
                    raise QueueOperationError(
                        reason=PlatformReason.INTEGRITY_FAILURE,
                        stage="queue.dead-letter.replay",
                    )
        except QueueOperationError:
            raise
        except DatabaseOperationError as error:
            raise QueueOperationError(
                reason=error.reason,
                stage="queue.dead-letter.replay",
                protected_cause=error,
            ) from None
        return QueueDeadLetterReplayReceipt(
            message_id=resolved_id,
            previous_attempts=previous_attempts,
            replayed_at=replayed_at,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._database.close()
        self._closed = True

    def _assert_open(self) -> None:
        if self._closed:
            raise QueueOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="queue.connection",
            )
        if os.getpid() != self._process_id:
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="queue.connection",
            )

    def _install(self) -> None:
        try:
            with self._database.transaction() as transaction:
                transaction.execute(
                    DatabaseStatement(
                        f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                            message_id VARCHAR(200) PRIMARY KEY,
                            message_type VARCHAR(128) NOT NULL,
                            schema_version INTEGER NOT NULL,
                            occurred_at VARCHAR(32) NOT NULL,
                            lane VARCHAR(128) NOT NULL,
                            available_at VARCHAR(32) NOT NULL,
                            envelope_text TEXT NOT NULL,
                            tags_text TEXT NULL,
                            attempt_count INTEGER NOT NULL DEFAULT 0,
                            reservation_generation INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL,
                            backoff_seconds REAL NULL,
                            reserved_at VARCHAR(32) NULL,
                            reservation_expires_at VARCHAR(32) NULL,
                            reservation_owner_id VARCHAR(64) NULL,
                            reservation_id VARCHAR(64) NULL,
                            dead_lettered_at VARCHAR(32) NULL
                        )"""
                    )
                )
            self._ensure_reservation_generation()
            self._ensure_reservation_expiry()
            self._ensure_reservation_owner()
            self._ensure_reservation_id()
            self._ensure_tags()
        except DatabaseOperationError as error:
            raise QueueOperationError(
                reason=error.reason,
                stage="queue.install",
                protected_cause=error,
            ) from None

    def _ensure_reservation_generation(self) -> None:
        probe = DatabaseStatement(
            f"SELECT reservation_generation FROM {_TABLE} WHERE 1 = 0",
            read_only=True,
        )
        try:
            with self._database.transaction() as transaction:
                transaction.query(probe)
            return
        except DatabaseOperationError:
            pass

        try:
            with self._database.transaction() as transaction:
                transaction.execute(
                    DatabaseStatement(
                        f"ALTER TABLE {_TABLE} ADD COLUMN "
                        "reservation_generation INTEGER NOT NULL DEFAULT 0"
                    )
                )
        except DatabaseOperationError:
            # A concurrent initializer may have completed the same migration.
            with self._database.transaction() as transaction:
                transaction.query(probe)

    def _ensure_reservation_expiry(self) -> None:
        probe = DatabaseStatement(
            f"SELECT reservation_expires_at FROM {_TABLE} WHERE 1 = 0",
            read_only=True,
        )
        try:
            with self._database.transaction() as transaction:
                transaction.query(probe)
            return
        except DatabaseOperationError:
            pass

        try:
            with self._database.transaction() as transaction:
                transaction.execute(
                    DatabaseStatement(
                        f"ALTER TABLE {_TABLE} ADD COLUMN reservation_expires_at VARCHAR(32) NULL"
                    )
                )
        except DatabaseOperationError:
            with self._database.transaction() as transaction:
                transaction.query(probe)

    def _ensure_reservation_owner(self) -> None:
        self._ensure_nullable_column("reservation_owner_id")

    def _ensure_reservation_id(self) -> None:
        self._ensure_nullable_column("reservation_id")

    def _ensure_tags(self) -> None:
        probe = DatabaseStatement(
            f"SELECT tags_text FROM {_TABLE} WHERE 1 = 0",
            read_only=True,
        )
        try:
            with self._database.transaction() as transaction:
                transaction.query(probe)
            return
        except DatabaseOperationError:
            pass
        try:
            with self._database.transaction() as transaction:
                transaction.execute(
                    DatabaseStatement(f"ALTER TABLE {_TABLE} ADD COLUMN tags_text TEXT NULL")
                )
        except DatabaseOperationError:
            with self._database.transaction() as transaction:
                transaction.query(probe)

    def _ensure_nullable_column(self, column: str) -> None:
        if column not in {"reservation_owner_id", "reservation_id"}:
            raise ValueError("database queue ownership column is invalid")
        probe = DatabaseStatement(
            f"SELECT {column} FROM {_TABLE} WHERE 1 = 0",
            read_only=True,
        )
        try:
            with self._database.transaction() as transaction:
                transaction.query(probe)
            return
        except DatabaseOperationError:
            pass
        try:
            with self._database.transaction() as transaction:
                transaction.execute(
                    DatabaseStatement(f"ALTER TABLE {_TABLE} ADD COLUMN {column} VARCHAR(64) NULL")
                )
        except DatabaseOperationError:
            with self._database.transaction() as transaction:
                transaction.query(probe)

    def _transition_reserved(
        self,
        reservation: object,
        *,
        stage: str,
        sql: str,
        available_at: datetime | None = None,
        terminal_at: datetime | None = None,
    ) -> None:
        if not isinstance(reservation, QueueReservation):
            action = stage.rsplit(".", 1)[-1]
            raise TypeError(f"database queue {action} requires a QueueReservation")
        if reservation.fencing_token is None:
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage=stage,
            )
        if reservation.expires_at is None:
            raise QueueReservationLost(stage=stage)
        if (
            reservation.owner_id is None
            or reservation.reservation_id is None
            or not secrets.compare_digest(reservation.owner_id, self._reservation_owner_id)
        ):
            raise QueueReservationLost(stage=stage)
        current = _aware_utc(self._clock(), "settlement time")
        parameters: dict[str, object] = {
            "message_id": reservation.job.envelope.metadata.message_id,
            "attempt": reservation.attempt,
            "previous_attempt": reservation.attempt - 1,
            "fencing_token": reservation.fencing_token,
            "reserved_at": _timestamp(reservation.reserved_at),
            "reservation_expires_at": _timestamp(reservation.expires_at),
            "now": _timestamp(current),
            "reservation_owner_id": self._reservation_owner_id,
            "reservation_id": reservation.reservation_id,
        }
        if available_at is not None:
            parameters["available_at"] = _timestamp(available_at)
        if terminal_at is not None:
            parameters["terminal_at"] = _timestamp(terminal_at)
        try:
            with self._database.transaction() as transaction:
                changed = transaction.execute(DatabaseStatement(sql, parameters))
        except DatabaseOperationError as error:
            raise QueueOperationError(
                reason=error.reason,
                stage=stage,
                protected_cause=error,
            ) from None
        if changed != 1:
            raise QueueReservationLost(stage=stage)

    def _new_token(self, *, stage: str) -> str:
        try:
            token = self._token_factory()
            if type(token) is not str or _OWNERSHIP_TOKEN.fullmatch(token) is None:
                raise ValueError("database queue ownership token is invalid")
            return token
        except Exception as error:
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage=stage,
                protected_cause=error,
            ) from None

    def _job_from_row(self, row: object) -> QueueJob:
        from af.framework.relational import DatabaseRow

        if not isinstance(row, DatabaseRow):
            raise _attempt_conflict()
        try:
            envelope = self._codec.decode(self._stored_text(row["envelope_text"]).encode("utf-8"))
            if not isinstance(envelope, MessageEnvelope):
                raise TypeError("queue envelope is invalid")
            return QueueJob(
                envelope=envelope,
                lane=self._stored_text(row["lane"]),
                max_attempts=self._stored_int(row["max_attempts"]),
                backoff=self._stored_backoff(row["backoff_seconds"]),
                tags=self._decode_tags(row["tags_text"]),
            )
        except Exception as error:
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="queue.integrity",
                protected_cause=error,
            ) from None

    @staticmethod
    def _stored_text(value: object) -> str:
        if type(value) is not str:
            raise TypeError("queue stored text is invalid")
        return value

    @staticmethod
    def _stored_int(value: object) -> int:
        if type(value) is not int:
            raise TypeError("queue stored integer is invalid")
        return value

    @staticmethod
    def _stored_backoff(value: object) -> timedelta | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise TypeError("queue stored backoff is invalid")
        return timedelta(seconds=float(value))

    @staticmethod
    def _encode_tags(tags: QueueJobTags | None) -> str | None:
        if tags is None:
            return None
        return json.dumps(
            {
                "agent_id": tags.agent_id,
                "capability_id": tags.capability_id,
                "correlation_id": tags.correlation_id,
                "task_id": tags.task_id,
                "tenant_ref": tags.tenant_ref,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _decode_tags(value: object) -> QueueJobTags | None:
        if value is None:
            return None
        if type(value) is not str or len(value.encode("utf-8")) > 2_048:
            raise TypeError("queue stored tags are invalid")
        decoded = json.loads(value)
        expected = {
            "agent_id",
            "capability_id",
            "correlation_id",
            "task_id",
            "tenant_ref",
        }
        if type(decoded) is not dict or set(decoded) != expected:
            raise TypeError("queue stored tags are invalid")
        return QueueJobTags(**decoded)
