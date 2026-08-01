"""Driver-neutral queue job, reservation, and receipt contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from af.framework.errors import PlatformError, PlatformReason
from af.messaging.envelopes import EnvelopeKind, MessageEnvelope

__all__ = (
    "QueueDispatchDisposition",
    "QueueDispatchReceipt",
    "QueueJob",
    "QueueOperationError",
    "QueueReservation",
    "UnsupportedQueueFeature",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")


class QueueOperationError(PlatformError):
    """Sanitized queue operation failure."""

    def __init__(
        self,
        *,
        reason: PlatformReason = PlatformReason.PERMANENT_FAILURE,
        stage: str = "queue.operation",
        protected_cause: BaseException | None = None,
    ) -> None:
        super().__init__(reason, stage=stage, protected_cause=protected_cause)


class UnsupportedQueueFeature(QueueOperationError):
    """A requested queue feature is not verified for the selected driver."""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(reason=PlatformReason.CAPABILITY_UNAVAILABLE, stage="queue.capability")


class QueueDispatchDisposition(StrEnum):
    """How the selected driver accepted the job."""

    INLINE = "inline"
    ENQUEUED = "enqueued"


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"queue {field} must be a canonical identifier")
    return value


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"queue {field} must be timezone-aware")
    return value.astimezone(UTC)


def _duration(value: object, field: str) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise ValueError(f"queue {field} must be a positive duration")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class QueueJob:
    """One immutable queue job plus bounded dispatch options."""

    envelope: MessageEnvelope
    lane: str = "default"
    delay: timedelta | None = None
    priority: int | None = None
    max_attempts: int = 1
    backoff: timedelta | None = None
    timeout: timedelta | None = None
    unique_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, MessageEnvelope):
            raise ValueError("queue job envelope is invalid")
        if self.envelope.metadata.kind is not EnvelopeKind.JOB:
            raise ValueError("queue job envelope must use the job kind")
        object.__setattr__(self, "lane", _identifier(self.lane, "lane"))
        if self.delay is not None:
            object.__setattr__(self, "delay", _duration(self.delay, "delay"))
        if self.priority is not None and type(self.priority) is not int:
            raise ValueError("queue priority must be an integer")
        if type(self.max_attempts) is not int or self.max_attempts < 1:
            raise ValueError("queue maximum attempts must be a positive integer")
        if self.backoff is not None:
            object.__setattr__(self, "backoff", _duration(self.backoff, "backoff"))
        if self.timeout is not None:
            object.__setattr__(self, "timeout", _duration(self.timeout, "timeout"))
        if self.unique_key is not None:
            object.__setattr__(self, "unique_key", _identifier(self.unique_key, "unique key"))

    def __repr__(self) -> str:
        return (
            f"QueueJob(message_id={self.envelope.metadata.message_id!r}, lane={self.lane!r}, "
            "envelope='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True)
class QueueDispatchReceipt:
    """Countable dispatch evidence that never exposes payload content."""

    message_id: str
    lane: str
    disposition: QueueDispatchDisposition
    dispatched_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", _identifier(self.message_id, "message id"))
        object.__setattr__(self, "lane", _identifier(self.lane, "lane"))
        if not isinstance(self.disposition, QueueDispatchDisposition):
            raise ValueError("queue dispatch disposition is invalid")
        object.__setattr__(self, "dispatched_at", _aware_utc(self.dispatched_at, "dispatch time"))


@dataclass(frozen=True, slots=True, repr=False)
class QueueReservation:
    """One reserved job delivery awaiting an acknowledgement outcome."""

    job: QueueJob
    attempt: int
    reserved_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.job, QueueJob):
            raise ValueError("queue reservation job is invalid")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("queue reservation attempt must be a positive integer")
        object.__setattr__(self, "reserved_at", _aware_utc(self.reserved_at, "reservation time"))

    def __repr__(self) -> str:
        return (
            f"QueueReservation(message_id={self.job.envelope.metadata.message_id!r}, "
            f"attempt={self.attempt!r}, reserved_at={self.reserved_at.isoformat()!r})"
        )
