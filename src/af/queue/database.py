"""Durable relational queue driver with bounded retry and dead-letter semantics."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from af.framework.errors import PlatformReason
from af.framework.relational import DatabaseDriver, DatabaseOperationError, DatabaseStatement
from af.messaging.envelopes import JsonEnvelopeCodec, MessageEnvelope
from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
    UnsupportedQueueFeature,
)
from af.queue.manager import QueueFeature

__all__ = ("DatabaseQueueDriver",)

_FEATURES = frozenset({QueueFeature.RESERVATION, QueueFeature.RETRY})
_TABLE = "af_queue_jobs"
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_MAX_ENVELOPE_BYTES = 1_048_576


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
    ) -> None:
        if not _is_database_driver(database):
            raise TypeError("database queue requires a DatabaseDriver")
        if not callable(clock):
            raise TypeError("database queue clock must be callable")
        resolved_codec = JsonEnvelopeCodec() if codec is None else codec
        if not isinstance(resolved_codec, JsonEnvelopeCodec):
            raise TypeError("database queue codec must be a JsonEnvelopeCodec")
        self._database = database
        self._clock = clock
        self._codec = resolved_codec
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
                            attempt_count,
                            max_attempts,
                            backoff_seconds,
                            reserved_at,
                            dead_lettered_at
                        ) VALUES (
                            :message_id,
                            :message_type,
                            :schema_version,
                            :occurred_at,
                            :lane,
                            :available_at,
                            :envelope_text,
                            0,
                            :max_attempts,
                            :backoff_seconds,
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
        if lane is not None:
            _identifier(lane, "lane")
        try:
            with self._database.transaction() as transaction:
                where = "available_at <= :now AND reserved_at IS NULL AND dead_lettered_at IS NULL"
                parameters: dict[str, object] = {"now": _timestamp(current)}
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
                            attempt_count,
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
                attempt = self._stored_int(row["attempt_count"]) + 1
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_TABLE}
                        SET attempt_count = :attempt_count,
                            reserved_at = :reserved_at
                        WHERE message_id = :message_id
                          AND attempt_count = :previous_attempt
                          AND reserved_at IS NULL
                          AND dead_lettered_at IS NULL""",
                        {
                            "attempt_count": attempt,
                            "reserved_at": _timestamp(current),
                            "message_id": message_id,
                            "previous_attempt": attempt - 1,
                        },
                    )
                )
                if changed != 1:
                    raise _attempt_conflict()
                return QueueReservation(
                    job=self._job_from_row(row),
                    attempt=attempt,
                    reserved_at=current,
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
                f"AND attempt_count = :attempt AND reserved_at = :reserved_at "
                f"AND dead_lettered_at IS NULL"
            ),
        )

    def reject(self, reservation: QueueReservation, /) -> None:
        self._assert_open()
        self._transition_reserved(
            reservation,
            stage="queue.reject",
            sql=(
                f"UPDATE {_TABLE} SET reserved_at = NULL, dead_lettered_at = :terminal_at "
                f"WHERE message_id = :message_id AND attempt_count = :attempt "
                f"AND reserved_at = :reserved_at AND dead_lettered_at IS NULL"
            ),
            terminal_at=_aware_utc(self._clock(), "dead-letter time"),
        )

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
                    f"UPDATE {_TABLE} SET reserved_at = NULL, dead_lettered_at = :terminal_at "
                    f"WHERE message_id = :message_id AND attempt_count = :attempt "
                    f"AND reserved_at = :reserved_at AND dead_lettered_at IS NULL"
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
                f"UPDATE {_TABLE} SET reserved_at = NULL, available_at = :available_at "
                f"WHERE message_id = :message_id AND attempt_count = :attempt "
                f"AND reserved_at = :reserved_at AND dead_lettered_at IS NULL"
            ),
            available_at=next_available,
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
                            attempt_count INTEGER NOT NULL DEFAULT 0,
                            max_attempts INTEGER NOT NULL,
                            backoff_seconds REAL NULL,
                            reserved_at VARCHAR(32) NULL,
                            dead_lettered_at VARCHAR(32) NULL
                        )"""
                    )
                )
        except DatabaseOperationError as error:
            raise QueueOperationError(
                reason=error.reason,
                stage="queue.install",
                protected_cause=error,
            ) from None

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
        parameters: dict[str, object] = {
            "message_id": reservation.job.envelope.metadata.message_id,
            "attempt": reservation.attempt,
            "reserved_at": _timestamp(reservation.reserved_at),
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
            raise _attempt_conflict()

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
