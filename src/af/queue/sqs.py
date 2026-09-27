"""SQS standard-queue adapter over an explicitly supplied, bounded SDK client.

Protocol tests cover AF semantics. A deployment must supply its own authenticated
client and run real-service conformance before treating this adapter as certified.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from math import ceil
from threading import RLock
from typing import Protocol
from urllib.parse import urlsplit

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

__all__ = ("SQSClient", "SQSQueueDriver")

_FEATURES = frozenset(
    {
        QueueFeature.RESERVATION,
        QueueFeature.RESERVATION_RENEWAL,
        QueueFeature.DELAY,
        QueueFeature.RETRY,
    }
)


class SQSClient(Protocol):
    """The boto3 SQS subset used by this adapter; configure finite SDK timeouts."""

    def send_message(self, **kwargs: object) -> Mapping[str, object]: ...
    def receive_message(self, **kwargs: object) -> Mapping[str, object]: ...
    def delete_message(self, **kwargs: object) -> object: ...
    def change_message_visibility(self, **kwargs: object) -> object: ...
    def close(self) -> None: ...


@dataclass(frozen=True, repr=False)
class _Delivery:
    reservation: QueueReservation
    queue_url: str
    receipt: str
    body: str


def _queues(values: Mapping[str, str]) -> dict[str, str]:
    snapshot = dict(values)
    if not snapshot:
        raise ValueError("SQS requires explicit logical queue lanes")
    for lane, url in snapshot.items():
        _identifier(lane, "lane")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or not parsed.path
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or url.endswith(".fifo")
        ):
            raise ValueError("SQS adapter requires standard HTTPS queue URLs")
    if len(set(snapshot.values())) != len(snapshot):
        raise ValueError("SQS lanes must address distinct physical queues")
    return snapshot


class SQSQueueDriver:
    """At-least-once SQS delivery with bounded attempts and explicit failed queues.

    Receipt handles remain private. Renewal rotates AF settlement handles. SQS
    release counts as a receive attempt, and dead-letter transfer is send then
    delete, so a crash can duplicate the failed copy without losing the source.
    """

    def __init__(
        self,
        *,
        client: SQSClient,
        queues: Mapping[str, str],
        dead_letter_queues: Mapping[str, str],
        clock: Callable[[], datetime] | None = None,
        visibility_timeout: int = 300,
    ) -> None:
        if type(visibility_timeout) is not int or not 1 <= visibility_timeout <= 43200:
            raise ValueError("SQS visibility timeout must be within 1..43200 seconds")
        self._queues = _queues(queues)
        self._dead = _queues(dead_letter_queues)
        if set(self._queues) != set(self._dead) or set(self._queues.values()) & set(
            self._dead.values()
        ):
            raise ValueError("SQS lanes require separate matching dead-letter queues")
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("SQS clock must be callable")
        self._visibility = visibility_timeout
        self._codec = QueueJobCodec()
        self._owner = secrets.token_hex(32)
        self._pid = os.getpid()
        self._deliveries: dict[str, _Delivery] = {}
        self._lock = RLock()
        self._closed = False

    @property
    def features(self) -> frozenset[QueueFeature]:
        return _FEATURES

    def supports(self, feature: QueueFeature, /) -> bool:
        return feature in _FEATURES

    def _now(self) -> datetime:
        return _aware_utc(self._clock(), "SQS clock")

    def _open(self) -> None:
        if self._closed or self._pid != os.getpid():
            raise QueueOperationError(stage="queue.sqs.connection")

    def _lane(self, lane: str | None) -> str:
        resolved = "default" if lane is None else _identifier(lane, "lane")
        if resolved not in self._queues:
            raise ValueError("SQS logical lane is not configured")
        return resolved

    def _call(self, method: str, **kwargs: object) -> object:
        try:
            operation = getattr(self._client, method)
            return operation(**kwargs)
        except Exception as error:
            raise QueueOperationError(
                reason=PlatformReason.TRANSIENT_FAILURE,
                stage=f"queue.sqs.{method.replace('_', '-')}",
                protected_cause=error,
            ) from None

    def _send(self, url: str, body: str, delay: int = 0) -> None:
        result = self._call("send_message", QueueUrl=url, MessageBody=body, DelaySeconds=delay)
        if not isinstance(result, Mapping) or not isinstance(result.get("MessageId"), str):
            raise QueueOperationError(stage="queue.sqs.send-message")

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt:
        if not isinstance(job, QueueJob):
            raise TypeError("SQS dispatch requires a QueueJob")
        for value, feature in (
            (job.priority, QueueFeature.PRIORITY),
            (job.timeout, QueueFeature.TIMEOUT),
            (job.unique_key, QueueFeature.UNIQUENESS),
            (job.fifo_group, QueueFeature.FIFO),
        ):
            if value is not None:
                raise UnsupportedQueueFeature(feature.value)
        if job.requires_transaction:
            raise UnsupportedQueueFeature(QueueFeature.TRANSACTIONAL_DISPATCH.value)
        delay = 0 if job.delay is None else ceil(job.delay.total_seconds())
        if delay > 900:
            raise ValueError("SQS per-message delay must not exceed 900 seconds")
        if job.backoff is not None and job.backoff.total_seconds() > 43200:
            raise ValueError("SQS backoff must not exceed 43200 seconds")
        with self._lock:
            self._open()
            lane = self._lane(job.lane)
            now = self._now()
            self._send(self._queues[lane], self._codec.encode(job).decode("utf-8"), delay)
            return QueueDispatchReceipt(
                job.envelope.metadata.message_id, lane, QueueDispatchDisposition.ENQUEUED, now
            )

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        with self._lock:
            self._open()
            lane = self._lane(lane)
            now = self._now()
            self._deliveries = {
                token: delivery
                for token, delivery in self._deliveries.items()
                if delivery.reservation.expires_at is not None
                and delivery.reservation.expires_at > now
            }
            response = self._call(
                "receive_message",
                QueueUrl=self._queues[lane],
                MaxNumberOfMessages=1,
                VisibilityTimeout=self._visibility,
                WaitTimeSeconds=0,
                MessageSystemAttributeNames=["ApproximateReceiveCount"],
            )
            if not isinstance(response, Mapping):
                raise QueueOperationError(stage="queue.sqs.receive-message")
            messages = response.get("Messages", [])
            if not isinstance(messages, list) or len(messages) > 1:
                raise QueueOperationError(stage="queue.sqs.receive-message")
            if not messages:
                return None
            message = messages[0]
            if not isinstance(message, Mapping):
                raise QueueOperationError(stage="queue.sqs.receive-message")
            body, receipt = message.get("Body"), message.get("ReceiptHandle")
            if not isinstance(body, str) or not isinstance(receipt, str) or not receipt:
                raise QueueOperationError(stage="queue.sqs.receive-message")
            try:
                job = self._codec.decode(body.encode("utf-8"))
                attributes = message.get("Attributes")
                if not isinstance(attributes, Mapping):
                    raise ValueError("SQS receive count is missing")
                raw_attempt = attributes.get("ApproximateReceiveCount")
                if type(raw_attempt) is not str or not raw_attempt.isdecimal():
                    raise ValueError("SQS receive count is invalid")
                attempt = int(raw_attempt)
                if attempt < 1 or job.lane != lane:
                    raise ValueError("SQS received job identity is invalid")
            except ValueError:
                self._bury(lane, receipt, body)
                return None
            if attempt > job.max_attempts:
                self._bury(lane, receipt, body)
                return None
            reservation = QueueReservation(
                job=job,
                attempt=attempt,
                reserved_at=now,
                fencing_token=attempt,
                expires_at=now + timedelta(seconds=self._visibility),
                owner_id=self._owner,
                reservation_id=secrets.token_hex(32),
            )
            assert reservation.reservation_id is not None
            self._deliveries[reservation.reservation_id] = _Delivery(
                reservation, self._queues[lane], receipt, body
            )
            return reservation

    def _owned(self, reservation: QueueReservation, stage: str) -> _Delivery:
        self._open()
        if not isinstance(reservation, QueueReservation):
            raise TypeError("SQS settlement requires a QueueReservation")
        delivery = self._deliveries.get(reservation.reservation_id or "")
        original = replace(reservation, connection_name=None, connection_binding=None)
        if (
            delivery is None
            or delivery.reservation != original
            or reservation.expires_at is None
            or reservation.expires_at <= self._now()
        ):
            raise QueueReservationLost(stage=f"queue.sqs.{stage}")
        return delivery

    def _forget(self, reservation: QueueReservation) -> None:
        self._deliveries.pop(reservation.reservation_id or "", None)

    def ack(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "ack")
            self._call(
                "delete_message", QueueUrl=delivery.queue_url, ReceiptHandle=delivery.receipt
            )
            self._forget(reservation)

    def _bury(self, lane: str, receipt: str, body: str) -> None:
        self._send(self._dead[lane], body)
        self._call("delete_message", QueueUrl=self._queues[lane], ReceiptHandle=receipt)

    def reject(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "reject")
            self._bury(reservation.job.lane, delivery.receipt, delivery.body)
            self._forget(reservation)

    def release(self, reservation: QueueReservation, /) -> None:
        with self._lock:
            delivery = self._owned(reservation, "release")
            self._call(
                "change_message_visibility",
                QueueUrl=delivery.queue_url,
                ReceiptHandle=delivery.receipt,
                VisibilityTimeout=0,
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
            now = self._now()
            when = (
                now + (reservation.job.backoff or timedelta(0))
                if available_at is None
                else _aware_utc(available_at, "retry time")
            )
            delay = max(0, ceil((when - now).total_seconds()))
            if delay > 43200:
                raise ValueError("SQS retry visibility must not exceed 43200 seconds")
            self._call(
                "change_message_visibility",
                QueueUrl=delivery.queue_url,
                ReceiptHandle=delivery.receipt,
                VisibilityTimeout=delay,
            )
            self._forget(reservation)

    def renew(self, reservation: QueueReservation, /) -> QueueReservation:
        with self._lock:
            delivery = self._owned(reservation, "renew")
            expires_at = self._now() + timedelta(seconds=self._visibility)
            if reservation.expires_at is None or expires_at <= reservation.expires_at:
                raise QueueReservationLost(stage="queue.sqs.renew")
            self._call(
                "change_message_visibility",
                QueueUrl=delivery.queue_url,
                ReceiptHandle=delivery.receipt,
                VisibilityTimeout=self._visibility,
            )
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
