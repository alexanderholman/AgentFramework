"""RabbitMQ quorum-queue adapter with confirmed publication and owned deliveries."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from threading import Event, RLock, Thread
from types import TracebackType
from typing import Protocol, Self

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

__all__ = ("RabbitMQChannel", "RabbitMQConnection", "RabbitMQQueueDriver")
_FEATURES = frozenset({QueueFeature.RESERVATION, QueueFeature.RETRY})


class RabbitMQChannel(Protocol):
    def confirm_delivery(self) -> None: ...
    def queue_declare(self, **kwargs: object) -> object: ...
    def basic_publish(self, **kwargs: object) -> object: ...
    def basic_get(self, **kwargs: object) -> tuple[object, object, object]: ...
    def basic_ack(self, **kwargs: object) -> object: ...
    def basic_nack(self, **kwargs: object) -> object: ...
    def basic_reject(self, **kwargs: object) -> object: ...


class RabbitMQConnection(Protocol):
    def channel(self) -> RabbitMQChannel: ...
    def close(self) -> None: ...
    def process_data_events(self, time_limit: float = 0) -> None: ...


@dataclass(frozen=True, repr=False)
class _Delivery:
    reservation: QueueReservation
    tag: int
    body: bytes


def _names(queues: Mapping[str, str]) -> dict[str, str]:
    result = dict(queues)
    if not result or len(set(result.values())) != len(result):
        raise ValueError("RabbitMQ lanes must map to distinct queues")
    for lane, queue in result.items():
        _identifier(lane, "lane")
        _identifier(queue, "broker queue")
    return result


class RabbitMQQueueDriver:
    """One owned blocking connection, marshalled onto one dedicated I/O thread.

    Uses durable quorum queues, publisher confirms and mandatory routing. Native
    failed-delivery counts bound retry, using basic.reject to register failures
    on RabbitMQ 4.3+ (basic.nack no longer increments that count). A deployment
    must provide finite connection/socket/blocked timeouts in its factory.
    """

    def __init__(
        self,
        *,
        connection_factory: Callable[[], RabbitMQConnection],
        properties_factory: Callable[..., object],
        queues: Mapping[str, str],
        dead_letter_queues: Mapping[str, str],
        delivery_limit: int = 20,
        heartbeat_interval: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._queues, self._dead = _names(queues), _names(dead_letter_queues)
        if set(self._queues) != set(self._dead) or set(self._queues.values()) & set(
            self._dead.values()
        ):
            raise ValueError("RabbitMQ lanes require separate matching failed queues")
        if type(delivery_limit) is not int or not 1 <= delivery_limit <= 10000:
            raise ValueError("RabbitMQ delivery limit must be within 1..10000")
        if not callable(connection_factory) or not callable(properties_factory):
            raise TypeError("RabbitMQ requires connection and properties factories")
        if not 0 < heartbeat_interval <= 60:
            raise ValueError("RabbitMQ heartbeat interval must be within 0..60 seconds")
        self._properties = properties_factory
        self._limit = delivery_limit
        self._clock = clock or (lambda: datetime.now(UTC))
        self._codec = QueueJobCodec()
        self._owner = secrets.token_hex(32)
        self._pid = os.getpid()
        self._closed = False
        self._lock = RLock()
        self._deliveries: dict[str, _Delivery] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="af-rabbitmq")
        try:
            self._connection, self._channel = self._executor.submit(
                self._connect, connection_factory
            ).result()
        except Exception as error:
            self._executor.shutdown(wait=True)
            raise QueueOperationError(
                stage="queue.rabbitmq.connect", protected_cause=error
            ) from None
        self._heartbeat_error: BaseException | None = None
        self._heartbeat_stop = Event()
        self._heartbeat = Thread(
            target=self._maintain_heartbeat,
            args=(heartbeat_interval,),
            daemon=True,
            name="af-rabbitmq-heartbeat",
        )
        self._heartbeat.start()

    def _maintain_heartbeat(self, interval: float) -> None:
        while not self._heartbeat_stop.wait(interval):
            try:
                self._executor.submit(self._connection.process_data_events, time_limit=0).result()
            except Exception as error:
                self._heartbeat_error = error
                return

    def _connect(
        self, factory: Callable[[], RabbitMQConnection]
    ) -> tuple[RabbitMQConnection, RabbitMQChannel]:
        connection = factory()
        try:
            channel = connection.channel()
            channel.confirm_delivery()
            for queue in self._dead.values():
                channel.queue_declare(
                    queue=queue, durable=True, arguments={"x-queue-type": "quorum"}
                )
            for lane, queue in self._queues.items():
                channel.queue_declare(
                    queue=queue,
                    durable=True,
                    arguments={
                        "x-queue-type": "quorum",
                        "x-delivery-limit": self._limit,
                        "x-dead-letter-exchange": "",
                        "x-dead-letter-routing-key": self._dead[lane],
                        "x-dead-letter-strategy": "at-least-once",
                        "x-overflow": "reject-publish",
                    },
                )
            return connection, channel
        except Exception:
            connection.close()
            raise

    @property
    def features(self) -> frozenset[QueueFeature]:
        return _FEATURES

    def supports(self, feature: QueueFeature, /) -> bool:
        return feature in _FEATURES

    def _open(self) -> None:
        if self._closed or self._pid != os.getpid():
            raise QueueOperationError(stage="queue.rabbitmq.connection")
        if self._heartbeat_error is not None:
            raise QueueOperationError(
                stage="queue.rabbitmq.heartbeat", protected_cause=self._heartbeat_error
            )

    def _now(self) -> datetime:
        return _aware_utc(self._clock(), "RabbitMQ clock")

    def _lane(self, lane: str | None) -> str:
        resolved = "default" if lane is None else _identifier(lane, "lane")
        if resolved not in self._queues:
            raise ValueError("RabbitMQ logical lane is not configured")
        return resolved

    def _call(self, method: str, **kwargs: object) -> object:
        try:
            return self._executor.submit(getattr(self._channel, method), **kwargs).result()
        except Exception as error:
            raise QueueOperationError(
                reason=PlatformReason.TRANSIENT_FAILURE,
                stage=f"queue.rabbitmq.{method.replace('_', '-')}",
                protected_cause=error,
            ) from None

    def _publish(self, queue: str, body: bytes) -> None:
        properties = self._properties(delivery_mode=2, content_type="application/json")
        result = self._call(
            "basic_publish",
            exchange="",
            routing_key=queue,
            body=body,
            properties=properties,
            mandatory=True,
        )
        if result is False:
            raise QueueOperationError(stage="queue.rabbitmq.publish-unconfirmed")

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt:
        if not isinstance(job, QueueJob):
            raise TypeError("RabbitMQ dispatch requires a QueueJob")
        for value, feature in (
            (job.delay, QueueFeature.DELAY),
            (job.priority, QueueFeature.PRIORITY),
            (job.timeout, QueueFeature.TIMEOUT),
            (job.unique_key, QueueFeature.UNIQUENESS),
            (job.fifo_group, QueueFeature.FIFO),
            (job.backoff, QueueFeature.DELAY),
        ):
            if value is not None:
                raise UnsupportedQueueFeature(feature.value)
        if job.requires_transaction:
            raise UnsupportedQueueFeature(QueueFeature.TRANSACTIONAL_DISPATCH.value)
        if job.max_attempts > self._limit:
            raise ValueError("job attempts exceed RabbitMQ delivery limit")
        with self._lock:
            self._open()
            lane = self._lane(job.lane)
            now = self._now()
            self._publish(self._queues[lane], self._codec.encode(job))
            return QueueDispatchReceipt(
                job.envelope.metadata.message_id, lane, QueueDispatchDisposition.ENQUEUED, now
            )

    def _bury(self, lane: str, tag: int, body: bytes) -> None:
        self._publish(self._dead[lane], body)
        self._call("basic_ack", delivery_tag=tag)

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        with self._lock:
            self._open()
            lane = self._lane(lane)
            result = self._call("basic_get", queue=self._queues[lane], auto_ack=False)
            if not isinstance(result, tuple) or len(result) != 3:
                raise QueueOperationError(stage="queue.rabbitmq.get")
            method, properties, body = result
            if method is None:
                return None
            tag = getattr(method, "delivery_tag", None)
            if type(tag) is not int or tag < 1 or type(body) is not bytes:
                raise QueueOperationError(stage="queue.rabbitmq.get")
            try:
                job = self._codec.decode(body)
                headers = getattr(properties, "headers", None) or {}
                if not isinstance(headers, Mapping):
                    raise ValueError("invalid RabbitMQ headers")
                failures = headers.get("x-delivery-count", 0)
                if (
                    type(failures) is not int
                    or failures < 0
                    or job.lane != lane
                    or (getattr(method, "redelivered", False) and "x-delivery-count" not in headers)
                ):
                    raise ValueError("RabbitMQ delivery count or lane is invalid")
                attempt = failures + 1
            except ValueError:
                self._bury(lane, tag, body)
                return None
            if attempt > min(job.max_attempts, self._limit):
                self._bury(lane, tag, body)
                return None
            reservation = QueueReservation(
                job=job,
                attempt=attempt,
                reserved_at=self._now(),
                fencing_token=tag,
                owner_id=self._owner,
                reservation_id=secrets.token_hex(32),
            )
            assert reservation.reservation_id is not None
            self._deliveries[reservation.reservation_id] = _Delivery(reservation, tag, body)
            return reservation

    def _owned(self, reservation: QueueReservation, stage: str) -> _Delivery:
        self._open()
        if not isinstance(reservation, QueueReservation):
            raise TypeError("RabbitMQ settlement requires a reservation")
        delivery = self._deliveries.get(reservation.reservation_id or "")
        if delivery is None or delivery.reservation != replace(
            reservation, connection_name=None, connection_binding=None
        ):
            raise QueueReservationLost(stage=f"queue.rabbitmq.{stage}")
        return delivery

    def _forget(self, reservation: QueueReservation) -> None:
        self._deliveries.pop(reservation.reservation_id or "", None)

    def ack(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "ack")
            self._call("basic_ack", delivery_tag=delivery.tag)
            self._forget(reservation)

    def reject(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "reject")
            self._bury(reservation.job.lane, delivery.tag, delivery.body)
            self._forget(reservation)

    def release(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "release")
            self._call("basic_nack", delivery_tag=delivery.tag, multiple=False, requeue=True)
            self._forget(reservation)

    def retry(
        self, reservation: QueueReservation, /, *, available_at: datetime | None = None
    ) -> None:
        with self._lock:
            delivery = self._owned(reservation, "retry")
            if available_at is not None and _aware_utc(available_at, "retry time") > self._now():
                raise UnsupportedQueueFeature(QueueFeature.DELAY.value)
            if reservation.attempt >= reservation.job.max_attempts:
                self.reject(reservation)
                return
            self._call("basic_reject", delivery_tag=delivery.tag, requeue=True)
            self._forget(reservation)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._heartbeat_stop.set()
            self._heartbeat.join()
            try:
                self._executor.submit(self._connection.close).result()
            except Exception as error:
                raise QueueOperationError(
                    stage="queue.rabbitmq.close", protected_cause=error
                ) from None
            finally:
                self._closed = True
                self._deliveries.clear()
                self._executor.shutdown(wait=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
