"""Beanstalkd adapter for an injected, exclusively owned Greenstalk-compatible client."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil
from threading import RLock
from typing import Protocol

from af.framework.errors import PlatformReason
from af.queue.codec import QueueJobCodec
from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
    QueueReservationLost,
    UnsupportedQueueFeature,
    _aware_utc,
    _identifier,
)
from af.queue.manager import QueueFeature

__all__ = ("BeanstalkdClient", "BeanstalkdQueueDriver")

_FEATURES = frozenset(
    {
        QueueFeature.RESERVATION,
        QueueFeature.RESERVATION_RENEWAL,
        QueueFeature.DELAY,
        QueueFeature.PRIORITY,
        QueueFeature.RETRY,
    }
)


class BeanstalkdClient(Protocol):
    """Greenstalk subset. Configure binary bodies and bounded socket timeouts."""

    def use(self, tube: str) -> None: ...
    def watch(self, tube: str) -> int: ...
    def ignore(self, tube: str) -> int: ...
    def watching(self) -> Sequence[str]: ...
    def put(self, body: bytes, *, priority: int, delay: int, ttr: int) -> int: ...
    def reserve(self, timeout: int) -> object: ...
    def stats_job(self, job: object) -> Mapping[str, object]: ...
    def delete(self, job: object) -> None: ...
    def release(self, job: object, *, priority: int, delay: int) -> None: ...
    def bury(self, job: object, *, priority: int) -> None: ...
    def touch(self, job: object) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, repr=False)
class _Delivery:
    reservation: QueueReservation
    broker_job: object
    lease_seconds: int


def _priority(priority: int | None) -> int:
    value = 0 if priority is None else priority
    if type(value) is not int or not -(2**31) <= value < 2**31:
        raise ValueError("Beanstalkd priority must fit a signed 32-bit integer")
    return 2**31 - 1 - value


def _delay(value: timedelta | None) -> int:
    seconds = 0 if value is None else max(0, ceil(value.total_seconds()))
    if seconds > 2**32 - 1:
        raise ValueError("Beanstalkd delay exceeds unsigned 32-bit seconds")
    return seconds


class BeanstalkdQueueDriver:
    """Native tube, priority, delay, TTR and burial semantics; at least once.

    The caller owns provisioning and persistence settings. Buried jobs stay in
    their source tube; release consumes a receive attempt. Protocol tests are not
    evidence that a particular deployed service is durable or available.
    """

    def __init__(
        self,
        *,
        client: BeanstalkdClient,
        tubes: Mapping[str, str],
        clock: Callable[[], datetime] | None = None,
        reservation_lease: int = 300,
        empty_errors: tuple[type[Exception], ...] = (TimeoutError,),
    ) -> None:
        self._tubes = dict(tubes)
        if not self._tubes or len(set(self._tubes.values())) != len(self._tubes):
            raise ValueError("Beanstalkd lanes require distinct explicit tubes")
        for lane, tube in self._tubes.items():
            _identifier(lane, "lane")
            _identifier(tube, "tube")
        if type(reservation_lease) is not int or not 1 <= reservation_lease <= 86400:
            raise ValueError("Beanstalkd reservation lease must be within one day")
        if not isinstance(empty_errors, tuple) or any(
            not isinstance(error, type) or not issubclass(error, Exception)
            for error in empty_errors
        ):
            raise ValueError("Beanstalkd empty responses require exception classes")
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lease = reservation_lease
        self._empty_errors = empty_errors
        self._codec = QueueJobCodec(max_bytes=65535)
        self._owner = secrets.token_hex(32)
        self._pid = os.getpid()
        self._lock = RLock()
        self._closed = False
        self._deliveries: dict[str, _Delivery] = {}

    @property
    def features(self) -> frozenset[QueueFeature]:
        return _FEATURES

    def supports(self, feature: QueueFeature, /) -> bool:
        return feature in _FEATURES

    def _open(self) -> None:
        if self._closed or os.getpid() != self._pid:
            raise QueueOperationError(stage="queue.beanstalkd.connection")

    def _now(self) -> datetime:
        return _aware_utc(self._clock(), "Beanstalkd clock")

    def _lane(self, lane: str | None) -> str:
        resolved = "default" if lane is None else _identifier(lane, "lane")
        if resolved not in self._tubes:
            raise ValueError("Beanstalkd logical lane is not configured")
        return resolved

    def _call(self, method: str, *args: object, **kwargs: object) -> object:
        try:
            return getattr(self._client, method)(*args, **kwargs)
        except self._empty_errors:
            if method == "reserve":
                return None
            raise QueueOperationError(stage=f"queue.beanstalkd.{method}") from None
        except Exception as error:
            raise QueueOperationError(
                reason=PlatformReason.TRANSIENT_FAILURE,
                stage=f"queue.beanstalkd.{method}",
                protected_cause=error,
            ) from None

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt:
        if not isinstance(job, QueueJob):
            raise TypeError("Beanstalkd dispatch requires a QueueJob")
        for value, feature in (
            (job.timeout, QueueFeature.TIMEOUT),
            (job.unique_key, QueueFeature.UNIQUENESS),
            (job.fifo_group, QueueFeature.FIFO),
        ):
            if value is not None:
                raise UnsupportedQueueFeature(feature.value)
        if job.requires_transaction:
            raise UnsupportedQueueFeature(QueueFeature.TRANSACTIONAL_DISPATCH.value)
        priority, delay = _priority(job.priority), _delay(job.delay)
        _delay(job.backoff)
        encoded = self._codec.encode(job)
        with self._lock:
            self._open()
            lane = self._lane(job.lane)
            now = self._now()
            self._call("use", self._tubes[lane])
            result = self._call("put", encoded, priority=priority, delay=delay, ttr=self._lease)
            if type(result) is not int or result < 1:
                raise QueueOperationError(stage="queue.beanstalkd.put")
            return QueueDispatchReceipt(
                job.envelope.metadata.message_id, lane, QueueDispatchDisposition.ENQUEUED, now
            )

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        with self._lock:
            self._open()
            lane = self._lane(lane)
            tube = self._tubes[lane]
            self._call("watch", tube)
            watching = self._call("watching")
            if not isinstance(watching, (tuple, list)) or any(
                type(item) is not str for item in watching
            ):
                raise QueueOperationError(stage="queue.beanstalkd.watching")
            for watched in watching:
                if watched != tube:
                    self._call("ignore", watched)
            now = self._now()
            self._deliveries = {
                token: delivery
                for token, delivery in self._deliveries.items()
                if delivery.reservation.expires_at is not None
                and delivery.reservation.expires_at > now
            }
            broker_job = self._call("reserve", timeout=0)
            if broker_job is None:
                return None
            try:
                body = getattr(broker_job, "body", None)
                if type(body) is not bytes:
                    raise ValueError("Beanstalkd client must return binary job bodies")
                job = self._codec.decode(body)
                stats = self._call("stats_job", broker_job)
                if not isinstance(stats, Mapping):
                    raise ValueError("Beanstalkd job statistics are invalid")
                attempt, ttr = stats.get("reserves"), stats.get("ttr")
                if (
                    type(attempt) is not int
                    or attempt < 1
                    or type(ttr) is not int
                    or ttr < 1
                    or job.lane != lane
                ):
                    raise ValueError("Beanstalkd job identity or lease is invalid")
                priority = _priority(job.priority)
            except ValueError:
                self._call("bury", broker_job, priority=2**31 - 1)
                return None
            if attempt > job.max_attempts:
                self._call("bury", broker_job, priority=priority)
                return None
            reservation = QueueReservation(
                job=job,
                attempt=attempt,
                reserved_at=now,
                fencing_token=attempt,
                expires_at=now + timedelta(seconds=ttr),
                owner_id=self._owner,
                reservation_id=secrets.token_hex(32),
            )
            assert reservation.reservation_id is not None
            self._deliveries[reservation.reservation_id] = _Delivery(reservation, broker_job, ttr)
            return reservation

    def _owned(self, reservation: QueueReservation, stage: str) -> _Delivery:
        self._open()
        if not isinstance(reservation, QueueReservation):
            raise TypeError("Beanstalkd settlement requires a QueueReservation")
        delivery = self._deliveries.get(reservation.reservation_id or "")
        if (
            delivery is None
            or delivery.reservation
            != replace(reservation, connection_name=None, connection_binding=None)
            or reservation.expires_at is None
            or reservation.expires_at <= self._now()
        ):
            raise QueueReservationLost(stage=f"queue.beanstalkd.{stage}")
        return delivery

    def _forget(self, reservation: QueueReservation) -> None:
        self._deliveries.pop(reservation.reservation_id or "", None)

    def ack(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "ack")
            self._call("delete", delivery.broker_job)
            self._forget(reservation)

    def reject(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "reject")
            self._call("bury", delivery.broker_job, priority=_priority(reservation.job.priority))
            self._forget(reservation)

    def release(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "release")
            self._call(
                "release",
                delivery.broker_job,
                priority=_priority(reservation.job.priority),
                delay=0,
            )
            self._forget(reservation)

    def retry(
        self, reservation: QueueReservation, /, *, available_at: datetime | None = None
    ) -> None:
        with self._lock:
            delivery = self._owned(reservation, "retry")
            if reservation.attempt >= reservation.job.max_attempts:
                self.reject(reservation)
                return
            delay = (
                reservation.job.backoff
                if available_at is None
                else _aware_utc(available_at, "retry time") - self._now()
            )
            self._call(
                "release",
                delivery.broker_job,
                priority=_priority(reservation.job.priority),
                delay=_delay(delay),
            )
            self._forget(reservation)

    def renew(self, reservation: QueueReservation, /) -> QueueReservation:
        with self._lock:
            delivery = self._owned(reservation, "renew")
            expires_at = self._now() + timedelta(seconds=delivery.lease_seconds)
            if reservation.expires_at is None or expires_at <= reservation.expires_at:
                raise QueueReservationLost(stage="queue.beanstalkd.renew")
            self._call("touch", delivery.broker_job)
            renewed = replace(
                delivery.reservation, expires_at=expires_at, reservation_id=secrets.token_hex(32)
            )
            self._forget(reservation)
            assert renewed.reservation_id is not None
            self._deliveries[renewed.reservation_id] = replace(delivery, reservation=renewed)
            return renewed

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._call("close")
            self._deliveries.clear()
            self._closed = True
