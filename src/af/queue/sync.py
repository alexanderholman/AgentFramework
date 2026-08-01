"""Synchronous inline queue driver for deterministic local execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from af.framework.errors import PlatformReason
from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
    UnsupportedQueueFeature,
)
from af.queue.manager import QueueFeature

__all__ = ("SyncQueueDriver",)

_FEATURES = frozenset({QueueFeature.INLINE_EXECUTION})


def _aware_utc(value: object, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"queue {field} must be timezone-aware")
    return value.astimezone(UTC)


def _unsupported_feature(job: QueueJob) -> QueueFeature | None:
    if job.delay is not None:
        return QueueFeature.DELAY
    if job.priority is not None:
        return QueueFeature.PRIORITY
    if job.max_attempts > 1 or job.backoff is not None:
        return QueueFeature.RETRY
    if job.timeout is not None:
        return QueueFeature.TIMEOUT
    if job.unique_key is not None:
        return QueueFeature.UNIQUENESS
    return None


class SyncQueueDriver:
    """Inline driver that executes accepted jobs immediately in-process."""

    def __init__(
        self,
        consumer: Callable[[QueueJob], None],
        *,
        clock: Callable[[], datetime],
    ) -> None:
        if not callable(consumer):
            raise TypeError("sync queue consumer must be callable")
        if not callable(clock):
            raise TypeError("sync queue clock must be callable")
        self._consumer = consumer
        self._clock = clock
        self._closed = False

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
            raise TypeError("sync queue dispatch requires a QueueJob")
        unsupported = _unsupported_feature(job)
        if unsupported is not None:
            raise UnsupportedQueueFeature(unsupported.value)
        dispatched_at = _aware_utc(self._clock(), "dispatch time")
        try:
            self._consumer(job)
        except QueueOperationError:
            raise
        except Exception as error:
            raise QueueOperationError(
                reason=PlatformReason.PERMANENT_FAILURE,
                stage="queue.dispatch",
                protected_cause=error,
            ) from None
        return QueueDispatchReceipt(
            message_id=job.envelope.metadata.message_id,
            lane=job.lane,
            disposition=QueueDispatchDisposition.INLINE,
            dispatched_at=dispatched_at,
        )

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        del lane
        raise UnsupportedQueueFeature(QueueFeature.RESERVATION.value)

    def ack(self, reservation: QueueReservation, /) -> None:
        del reservation
        raise UnsupportedQueueFeature(QueueFeature.RESERVATION.value)

    def reject(self, reservation: QueueReservation, /) -> None:
        del reservation
        raise UnsupportedQueueFeature(QueueFeature.RESERVATION.value)

    def retry(
        self,
        reservation: QueueReservation,
        /,
        *,
        available_at: datetime | None = None,
    ) -> None:
        del reservation, available_at
        raise UnsupportedQueueFeature(QueueFeature.RETRY.value)

    def close(self) -> None:
        self._closed = True

    def _assert_open(self) -> None:
        if self._closed:
            raise QueueOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="queue.connection",
            )
