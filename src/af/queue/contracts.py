"""Driver-neutral queue job, reservation, and receipt contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from af.framework.errors import PlatformError, PlatformReason
from af.messaging.envelopes import EnvelopeKind, MessageEnvelope

__all__ = (
    "QueueDeadLetter",
    "QueueDeadLetterReplayReceipt",
    "QueueDispatchDisposition",
    "QueueDispatchReceipt",
    "QueueJob",
    "QueueJobTags",
    "QueueOperationError",
    "QueueReservation",
    "QueueReservationLost",
    "UnsupportedQueueFeature",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_OWNERSHIP_TOKEN = re.compile(r"^[0-9a-f]{64}$")
_TENANT_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


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


class QueueReservationLost(QueueOperationError):
    """The caller no longer owns the reserved delivery generation."""

    def __init__(self, *, stage: str) -> None:
        super().__init__(reason=PlatformReason.INTEGRITY_FAILURE, stage=stage)


class QueueDispatchDisposition(StrEnum):
    """How the selected driver accepted the job."""

    INLINE = "inline"
    ENQUEUED = "enqueued"


@dataclass(frozen=True, slots=True, repr=False)
class QueueJobTags:
    """Typed worker identities with redacted correlation and tenant context."""

    correlation_id: str | None = None
    tenant_ref: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    capability_id: str | None = None

    def __post_init__(self) -> None:
        if self.correlation_id is not None:
            _opaque_tag(self.correlation_id, "tag correlation id")
        if self.tenant_ref is not None and (
            type(self.tenant_ref) is not str or _TENANT_REF.fullmatch(self.tenant_ref) is None
        ):
            raise ValueError("queue tag tenant reference must be an opaque sha256 digest")
        for value, field in (
            (self.agent_id, "tag agent id"),
            (self.task_id, "tag task id"),
            (self.capability_id, "tag capability id"),
        ):
            if value is not None:
                _identifier(value, field)

    @classmethod
    def from_envelope(
        cls,
        envelope: MessageEnvelope,
        *,
        agent_id: str | None = None,
        task_id: str | None = None,
        capability_id: str | None = None,
    ) -> QueueJobTags:
        """Copy security context from an envelope without inspecting its payload."""

        if not isinstance(envelope, MessageEnvelope):
            raise TypeError("queue job tags require a MessageEnvelope")
        metadata = envelope.metadata
        telemetry = metadata.telemetry
        correlation_id = metadata.correlation_id or (
            None if telemetry is None else telemetry.correlation_id
        )
        policy = metadata.policy
        tenant_ref = (None if policy is None else policy.tenant_ref) or (
            None if telemetry is None else telemetry.tenant_ref
        )
        return cls(
            correlation_id=correlation_id,
            tenant_ref=tenant_ref,
            agent_id=agent_id,
            task_id=task_id,
            capability_id=capability_id,
        )

    def __repr__(self) -> str:
        return (
            "QueueJobTags(correlation_id='[REDACTED]', tenant_ref='[REDACTED]', "
            "agent_id='[REDACTED]', task_id='[REDACTED]', "
            "capability_id='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True, repr=False)
class QueueDeadLetter:
    """Inspectable terminal delivery retaining its redacted immutable job."""

    job: QueueJob
    attempts: int
    dead_lettered_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.job, QueueJob):
            raise ValueError("queue dead letter job is invalid")
        if type(self.attempts) is not int or self.attempts < 1:
            raise ValueError("queue dead letter attempts must be positive")
        object.__setattr__(
            self,
            "dead_lettered_at",
            _aware_utc(self.dead_lettered_at, "dead-letter time"),
        )

    @property
    def message_id(self) -> str:
        return self.job.envelope.metadata.message_id

    @property
    def lane(self) -> str:
        return self.job.lane

    def __repr__(self) -> str:
        return (
            f"QueueDeadLetter(message_id={self.message_id!r}, lane={self.lane!r}, "
            f"attempts={self.attempts!r}, "
            f"dead_lettered_at={self.dead_lettered_at.isoformat()!r}, "
            "job='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True)
class QueueDeadLetterReplayReceipt:
    """Countable evidence that one terminal delivery was made eligible again."""

    message_id: str
    previous_attempts: int
    replayed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", _identifier(self.message_id, "message id"))
        if type(self.previous_attempts) is not int or self.previous_attempts < 1:
            raise ValueError("queue replay previous attempts must be positive")
        object.__setattr__(self, "replayed_at", _aware_utc(self.replayed_at, "replay time"))


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


def _opaque_tag(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 200 or _UNSAFE_TEXT.search(value):
        raise ValueError(f"queue {field} must be bounded non-empty single-line text")
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
    fifo_group: str | None = None
    requires_transaction: bool = False
    tags: QueueJobTags | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.envelope, MessageEnvelope):
            raise ValueError("queue job envelope is invalid")
        if self.envelope.metadata.kind is not EnvelopeKind.JOB:
            raise ValueError("queue job envelope must use the job kind")
        _identifier(self.envelope.metadata.message_id, "message id")
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
        if self.fifo_group is not None:
            object.__setattr__(self, "fifo_group", _identifier(self.fifo_group, "fifo group"))
        if type(self.requires_transaction) is not bool:
            raise ValueError("queue requires transaction must be a boolean")
        if self.tags is not None:
            if not isinstance(self.tags, QueueJobTags):
                raise ValueError("queue job tags are invalid")
            expected = QueueJobTags.from_envelope(self.envelope)
            if self.tags.correlation_id != expected.correlation_id:
                raise ValueError("queue job tag correlation context must match its envelope")
            if self.tags.tenant_ref != expected.tenant_ref:
                raise ValueError("queue job tag tenant context must match its envelope")

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
    fencing_token: int | None = None
    expires_at: datetime | None = None
    owner_id: str | None = None
    reservation_id: str | None = None
    connection_name: str | None = None
    connection_binding: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job, QueueJob):
            raise ValueError("queue reservation job is invalid")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("queue reservation attempt must be a positive integer")
        object.__setattr__(self, "reserved_at", _aware_utc(self.reserved_at, "reservation time"))
        if self.fencing_token is not None and (
            type(self.fencing_token) is not int or self.fencing_token < 1
        ):
            raise ValueError("queue reservation fencing token must be a positive integer")
        if self.expires_at is not None:
            expires_at = _aware_utc(self.expires_at, "reservation expiry")
            if expires_at <= self.reserved_at:
                raise ValueError("queue reservation expiry must follow reservation time")
            object.__setattr__(self, "expires_at", expires_at)
        if (self.owner_id is None) != (self.reservation_id is None):
            raise ValueError("queue reservation ownership identity must be complete")
        for value, field in (
            (self.owner_id, "owner"),
            (self.reservation_id, "acquisition"),
        ):
            if value is not None and (
                type(value) is not str or _OWNERSHIP_TOKEN.fullmatch(value) is None
            ):
                raise ValueError(f"queue reservation {field} identity is invalid")
        if self.connection_name is not None:
            object.__setattr__(
                self,
                "connection_name",
                _identifier(self.connection_name, "connection name"),
            )
        if (self.connection_name is None) != (self.connection_binding is None):
            raise ValueError("queue reservation connection binding must be complete")
        if self.connection_binding is not None and (
            type(self.connection_binding) is not str
            or _OWNERSHIP_TOKEN.fullmatch(self.connection_binding) is None
        ):
            raise ValueError("queue reservation connection binding is invalid")

    def __repr__(self) -> str:
        return (
            f"QueueReservation(message_id={self.job.envelope.metadata.message_id!r}, "
            f"attempt={self.attempt!r}, reserved_at={self.reserved_at.isoformat()!r}, "
            f"fencing_token={self.fencing_token!r}, expires_at={self.expires_at!r}, "
            "ownership='[REDACTED]', connection_binding='[REDACTED]')"
        )
