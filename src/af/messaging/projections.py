"""Transactional inbox and replay-safe projection checkpoint storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    DatabaseTransaction,
)

__all__ = (
    "InboxRecord",
    "ProjectionCheckpoint",
    "ProjectionCheckpointStore",
    "ProjectionDecision",
    "ProjectionGapError",
    "ProjectionIntegrityError",
    "TransactionalInbox",
    "TransactionalProjectionGuard",
)

_INBOX_TABLE = "af_inbox"
_CHECKPOINT_TABLE = "af_projection_checkpoints"
_MAX_POSITION = (1 << 63) - 1
_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")


class ProjectionIntegrityError(DatabaseOperationError):
    """Stored delivery evidence or checkpoint state is internally inconsistent."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            reason=PlatformReason.INTEGRITY_FAILURE,
            stage="projection.integrity",
            protected_cause=protected_cause,
        )


class ProjectionGapError(DatabaseOperationError):
    """A projection delivery skipped one or more required source positions."""

    def __init__(self) -> None:
        super().__init__(
            reason=PlatformReason.CONTEXT_UNAVAILABLE,
            stage="projection.gap",
        )


class ProjectionDecision(StrEnum):
    """How a guarded projection transaction must treat one delivery."""

    APPLY = "apply"
    DUPLICATE = "duplicate"
    STALE = "stale"


@dataclass(frozen=True, slots=True, repr=False)
class InboxRecord:
    """One consumer-scoped record of a processed or deliberately skipped delivery."""

    consumer_name: str
    source_name: str
    message_id: str
    position: int
    processed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "consumer_name", _name(self.consumer_name, "inbox consumer"))
        object.__setattr__(self, "source_name", _name(self.source_name, "inbox source"))
        object.__setattr__(self, "message_id", _message_id(self.message_id))
        object.__setattr__(self, "position", _position(self.position))
        object.__setattr__(
            self,
            "processed_at",
            _aware_utc(self.processed_at, "inbox processed time"),
        )

    def __repr__(self) -> str:
        return (
            "InboxRecord(consumer_name='[REDACTED]', source_name='[REDACTED]', "
            "message_id='[REDACTED]', "
            f"position={self.position!r}, processed_at={self.processed_at.isoformat()!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProjectionCheckpoint:
    """The latest contiguous source position committed by one projection."""

    projection_name: str
    source_name: str
    position: int
    message_id: str
    updated_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "projection_name",
            _name(self.projection_name, "projection name"),
        )
        object.__setattr__(self, "source_name", _name(self.source_name, "projection source"))
        object.__setattr__(self, "position", _position(self.position))
        object.__setattr__(self, "message_id", _message_id(self.message_id))
        object.__setattr__(
            self,
            "updated_at",
            _aware_utc(self.updated_at, "projection update time"),
        )

    def __repr__(self) -> str:
        return (
            "ProjectionCheckpoint(projection_name='[REDACTED]', "
            "source_name='[REDACTED]', message_id='[REDACTED]', "
            f"position={self.position!r}, updated_at={self.updated_at.isoformat()!r})"
        )


class TransactionalInbox:
    """Consumer-scoped duplicate evidence stored in a caller-owned transaction."""

    def install(self, transaction: DatabaseTransaction, /) -> None:
        transaction.execute(
            DatabaseStatement(
                f"""CREATE TABLE IF NOT EXISTS {_INBOX_TABLE} (
                    consumer_name VARCHAR(128) NOT NULL,
                    source_name VARCHAR(128) NOT NULL,
                    message_id VARCHAR(200) NOT NULL,
                    position_value BIGINT NOT NULL,
                    processed_at VARCHAR(32) NOT NULL,
                    PRIMARY KEY (consumer_name, source_name, message_id)
                )"""
            )
        )

    def find(
        self,
        transaction: DatabaseTransaction,
        /,
        *,
        consumer_name: str,
        source_name: str,
        message_id: str,
    ) -> InboxRecord | None:
        consumer = _name(consumer_name, "inbox consumer")
        source = _name(source_name, "inbox source")
        identifier = _message_id(message_id)
        rows = transaction.query(
            DatabaseStatement(
                f"""SELECT
                    consumer_name,
                    source_name,
                    message_id,
                    position_value,
                    processed_at
                FROM {_INBOX_TABLE}
                WHERE consumer_name = :consumer_name
                  AND source_name = :source_name
                  AND message_id = :message_id""",
                {
                    "consumer_name": consumer,
                    "source_name": source,
                    "message_id": identifier,
                },
                read_only=True,
            )
        )
        if len(rows) > 1:
            raise ProjectionIntegrityError()
        if not rows:
            return None
        return self._record(rows[0], consumer, source, identifier)

    def record(self, transaction: DatabaseTransaction, record: InboxRecord, /) -> None:
        if not isinstance(record, InboxRecord):
            raise TypeError("inbox record must be an InboxRecord")
        transaction.execute(
            DatabaseStatement(
                f"""INSERT INTO {_INBOX_TABLE} (
                    consumer_name,
                    source_name,
                    message_id,
                    position_value,
                    processed_at
                ) VALUES (
                    :consumer_name,
                    :source_name,
                    :message_id,
                    :position_value,
                    :processed_at
                )""",
                {
                    "consumer_name": record.consumer_name,
                    "source_name": record.source_name,
                    "message_id": record.message_id,
                    "position_value": record.position,
                    "processed_at": _timestamp(record.processed_at),
                },
            )
        )

    @staticmethod
    def _record(
        row: DatabaseRow,
        consumer_name: str,
        source_name: str,
        message_id: str,
    ) -> InboxRecord:
        try:
            record = InboxRecord(
                consumer_name=_stored_text(row["consumer_name"]),
                source_name=_stored_text(row["source_name"]),
                message_id=_stored_text(row["message_id"]),
                position=_stored_int(row["position_value"]),
                processed_at=_parse_timestamp(_stored_text(row["processed_at"])),
            )
            if (
                record.consumer_name != consumer_name
                or record.source_name != source_name
                or record.message_id != message_id
            ):
                raise ValueError("inbox row identity diverges from its key")
        except (KeyError, TypeError, ValueError) as error:
            raise ProjectionIntegrityError(protected_cause=error) from None
        return record


class ProjectionCheckpointStore:
    """Optimistic contiguous checkpoints stored in a caller-owned transaction."""

    def install(self, transaction: DatabaseTransaction, /) -> None:
        transaction.execute(
            DatabaseStatement(
                f"""CREATE TABLE IF NOT EXISTS {_CHECKPOINT_TABLE} (
                    projection_name VARCHAR(128) NOT NULL,
                    source_name VARCHAR(128) NOT NULL,
                    position_value BIGINT NOT NULL,
                    message_id VARCHAR(200) NOT NULL,
                    updated_at VARCHAR(32) NOT NULL,
                    PRIMARY KEY (projection_name, source_name)
                )"""
            )
        )

    def load(
        self,
        transaction: DatabaseTransaction,
        /,
        *,
        projection_name: str,
        source_name: str,
    ) -> ProjectionCheckpoint | None:
        projection = _name(projection_name, "projection name")
        source = _name(source_name, "projection source")
        rows = transaction.query(
            DatabaseStatement(
                f"""SELECT
                    projection_name,
                    source_name,
                    position_value,
                    message_id,
                    updated_at
                FROM {_CHECKPOINT_TABLE}
                WHERE projection_name = :projection_name
                  AND source_name = :source_name""",
                {
                    "projection_name": projection,
                    "source_name": source,
                },
                read_only=True,
            )
        )
        if len(rows) > 1:
            raise ProjectionIntegrityError()
        if not rows:
            return None
        return self._checkpoint(rows[0], projection, source)

    def start(self, transaction: DatabaseTransaction, checkpoint: ProjectionCheckpoint, /) -> None:
        if not isinstance(checkpoint, ProjectionCheckpoint):
            raise TypeError("projection checkpoint must be a ProjectionCheckpoint")
        transaction.execute(
            DatabaseStatement(
                f"""INSERT INTO {_CHECKPOINT_TABLE} (
                    projection_name,
                    source_name,
                    position_value,
                    message_id,
                    updated_at
                ) VALUES (
                    :projection_name,
                    :source_name,
                    :position_value,
                    :message_id,
                    :updated_at
                )""",
                {
                    "projection_name": checkpoint.projection_name,
                    "source_name": checkpoint.source_name,
                    "position_value": checkpoint.position,
                    "message_id": checkpoint.message_id,
                    "updated_at": _timestamp(checkpoint.updated_at),
                },
            )
        )

    def advance(
        self,
        transaction: DatabaseTransaction,
        previous: ProjectionCheckpoint,
        current: ProjectionCheckpoint,
        /,
    ) -> None:
        if not isinstance(previous, ProjectionCheckpoint) or not isinstance(
            current,
            ProjectionCheckpoint,
        ):
            raise TypeError("projection checkpoint transition is invalid")
        if (
            previous.projection_name != current.projection_name
            or previous.source_name != current.source_name
            or current.position != previous.position + 1
        ):
            raise ValueError("projection checkpoint transition must be contiguous")
        changed = transaction.execute(
            DatabaseStatement(
                f"""UPDATE {_CHECKPOINT_TABLE}
                SET position_value = :current_position,
                    message_id = :current_message_id,
                    updated_at = :current_updated_at
                WHERE projection_name = :projection_name
                  AND source_name = :source_name
                  AND position_value = :previous_position
                  AND message_id = :previous_message_id""",
                {
                    "current_position": current.position,
                    "current_message_id": current.message_id,
                    "current_updated_at": _timestamp(current.updated_at),
                    "projection_name": current.projection_name,
                    "source_name": current.source_name,
                    "previous_position": previous.position,
                    "previous_message_id": previous.message_id,
                },
            )
        )
        if type(changed) is not int or changed != 1:
            raise ProjectionIntegrityError()

    @staticmethod
    def _checkpoint(
        row: DatabaseRow,
        projection_name: str,
        source_name: str,
    ) -> ProjectionCheckpoint:
        try:
            checkpoint = ProjectionCheckpoint(
                projection_name=_stored_text(row["projection_name"]),
                source_name=_stored_text(row["source_name"]),
                position=_stored_int(row["position_value"]),
                message_id=_stored_text(row["message_id"]),
                updated_at=_parse_timestamp(_stored_text(row["updated_at"])),
            )
            if (
                checkpoint.projection_name != projection_name
                or checkpoint.source_name != source_name
            ):
                raise ValueError("projection checkpoint identity diverges from its key")
        except (KeyError, TypeError, ValueError) as error:
            raise ProjectionIntegrityError(protected_cause=error) from None
        return checkpoint


class TransactionalProjectionGuard:
    """Atomically classify delivery and reserve its projection checkpoint.

    The caller must apply projection mutations only for ``APPLY`` and in the same transaction.
    Commit then records the inbox evidence, checkpoint, and projection state together. Any failure
    rolls them all back. The first accepted source position establishes the baseline; later accepted
    positions must be contiguous.
    """

    def __init__(
        self,
        *,
        inbox: TransactionalInbox | None = None,
        checkpoints: ProjectionCheckpointStore | None = None,
    ) -> None:
        resolved_inbox = TransactionalInbox() if inbox is None else inbox
        resolved_checkpoints = ProjectionCheckpointStore() if checkpoints is None else checkpoints
        if not isinstance(resolved_inbox, TransactionalInbox):
            raise TypeError("projection inbox must be a TransactionalInbox")
        if not isinstance(resolved_checkpoints, ProjectionCheckpointStore):
            raise TypeError("projection checkpoints must be a ProjectionCheckpointStore")
        self._inbox = resolved_inbox
        self._checkpoints = resolved_checkpoints

    def install(self, transaction: DatabaseTransaction, /) -> None:
        self._inbox.install(transaction)
        self._checkpoints.install(transaction)

    def prepare(
        self,
        transaction: DatabaseTransaction,
        /,
        *,
        projection_name: str,
        source_name: str,
        message_id: str,
        position: int,
        processed_at: datetime,
    ) -> ProjectionDecision:
        candidate = InboxRecord(
            consumer_name=projection_name,
            source_name=source_name,
            message_id=message_id,
            position=position,
            processed_at=processed_at,
        )
        seen = self._inbox.find(
            transaction,
            consumer_name=candidate.consumer_name,
            source_name=candidate.source_name,
            message_id=candidate.message_id,
        )
        if seen is not None and seen.position != candidate.position:
            raise ProjectionIntegrityError()
        if seen is not None:
            return ProjectionDecision.DUPLICATE

        previous = self._checkpoints.load(
            transaction,
            projection_name=candidate.consumer_name,
            source_name=candidate.source_name,
        )
        current = ProjectionCheckpoint(
            projection_name=candidate.consumer_name,
            source_name=candidate.source_name,
            position=candidate.position,
            message_id=candidate.message_id,
            updated_at=candidate.processed_at,
        )
        if previous is None:
            self._checkpoints.start(transaction, current)
            self._inbox.record(transaction, candidate)
            return ProjectionDecision.APPLY

        if current.position < previous.position:
            self._inbox.record(transaction, candidate)
            return ProjectionDecision.STALE

        if current.position == previous.position and current.message_id != previous.message_id:
            raise ProjectionIntegrityError()
        if current.position == previous.position:
            self._inbox.record(transaction, candidate)
            return ProjectionDecision.DUPLICATE

        if current.position != previous.position + 1:
            raise ProjectionGapError()

        self._checkpoints.advance(transaction, previous, current)
        self._inbox.record(transaction, candidate)
        return ProjectionDecision.APPLY


def _name(value: str, field: str) -> str:
    if type(value) is not str or _NAME.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical identifier")
    return value


def _message_id(value: str) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 200
        or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value)
    ):
        raise ValueError("projection message id is invalid")
    return value


def _position(value: int) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_POSITION:
        raise ValueError("projection position is invalid")
    return value


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    if not value.endswith("Z"):
        raise ValueError("projection timestamp is not canonical UTC")
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    normalized = _aware_utc(parsed, "projection stored time")
    if _timestamp(normalized) != value:
        raise ValueError("projection timestamp is not canonical UTC")
    return normalized


def _stored_text(value: object) -> str:
    if type(value) is not str:
        raise TypeError("projection stored text is invalid")
    return value


def _stored_int(value: object) -> int:
    if type(value) is not int:
        raise TypeError("projection stored integer is invalid")
    return value
