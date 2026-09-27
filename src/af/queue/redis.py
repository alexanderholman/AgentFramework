"""Redis queue using watched transactions, server time and fenced reservations.

The injected client must have finite socket timeouts and use an exclusively owned
namespace on a non-evicting Redis deployment. Persistence follows server settings.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from af.framework.errors import PlatformReason
from af.queue.codec import QueueJobCodec
from af.queue.contracts import (
    QueueDeadLetter,
    QueueDeadLetterReplayReceipt,
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

__all__ = ("RedisQueueClient", "RedisQueueDriver", "RedisQueuePipeline")

_FEATURES = frozenset(
    {
        QueueFeature.RESERVATION,
        QueueFeature.RESERVATION_RENEWAL,
        QueueFeature.DELAY,
        QueueFeature.RETRY,
        QueueFeature.DEAD_LETTER_INSPECTION,
        QueueFeature.DEAD_LETTER_REPLAY,
    }
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class RedisQueuePipeline(Protocol):
    """Redis-py synchronous WATCH/MULTI subset; responses may be bytes or text."""

    def watch(self, *keys: str) -> object: ...
    def reset(self) -> None: ...
    def type(self, key: str) -> object: ...
    def time(self) -> object: ...
    def hget(self, key: str, field: str) -> object: ...
    def hset(self, key: str, field: str, value: str) -> object: ...
    def hdel(self, key: str, field: str) -> object: ...
    def zrangebyscore(
        self, key: str, low: str, high: int | str, *, start: int, num: int
    ) -> object: ...
    def zadd(self, key: str, values: Mapping[str, float]) -> object: ...
    def zrem(self, key: str, member: str) -> object: ...
    def multi(self) -> None: ...
    def execute(self) -> object: ...


class RedisQueueClient(Protocol):
    """Authenticated Redis client supplied by deployment composition."""

    def pipeline(self, *, transaction: bool = True) -> RedisQueuePipeline: ...
    def close(self) -> None: ...


def _micros(value: datetime) -> int:
    delta = value - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _date(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if type(value) is str:
        return value
    raise ValueError("Redis queue response is not text")


@dataclass(frozen=True, repr=False)
class _Record:
    job: QueueJob
    attempt: int = 0
    generation: int = 0
    status: str = "ready"
    available: int = 0
    reserved: int = 0
    expiry: int = 0
    owner: str = ""
    token: str = ""


class RedisQueueDriver:
    """Atomic at-least-once delivery with durable attempts and explicit replay.

    Transactions watch the complete namespace, favoring simple integrity over high
    contention throughput. No handler or external effect is covered by a Redis
    transaction. Cluster clients are not certified by this adapter.
    """

    def __init__(
        self,
        *,
        client: RedisQueueClient,
        namespace: str,
        lanes: Iterable[str] = ("default",),
        reservation_lease: timedelta = timedelta(minutes=5),
        conflict_errors: tuple[type[Exception], ...] = (),
        transaction_attempts: int = 16,
    ) -> None:
        _identifier(namespace, "namespace")
        if isinstance(lanes, str):
            raise ValueError("Redis lanes require an explicit collection")
        self._lanes = tuple(lanes)
        if not self._lanes or len(self._lanes) > 32 or len(set(self._lanes)) != len(self._lanes):
            raise ValueError("Redis requires 1..32 distinct lanes")
        for lane in self._lanes:
            _identifier(lane, "lane")
        if (
            not isinstance(reservation_lease, timedelta)
            or reservation_lease <= timedelta(0)
            or reservation_lease > timedelta(days=1)
        ):
            raise ValueError("Redis reservation lease must be within one day")
        if type(transaction_attempts) is not int or not 1 <= transaction_attempts <= 100:
            raise ValueError("Redis transaction attempts must be within 1..100")
        if not isinstance(conflict_errors, tuple) or any(
            not isinstance(error, type) or not issubclass(error, Exception)
            for error in conflict_errors
        ):
            raise TypeError("Redis conflict errors must be exception classes")
        digest = hashlib.sha256(namespace.encode()).hexdigest()
        self._prefix = f"af:queue:{{{digest}}}:v1"
        self._jobs = f"{self._prefix}:jobs"
        self._keys = (
            self._jobs,
            *(
                self._key(lane, status)
                for lane in self._lanes
                for status in ("ready", "reserved", "failed")
            ),
        )
        self._client = client
        self._lease = reservation_lease
        self._conflicts = conflict_errors
        self._attempts = transaction_attempts
        self._codec = QueueJobCodec()
        self._owner = secrets.token_hex(32)
        self._pid = os.getpid()
        self._closed = False

    @property
    def features(self) -> frozenset[QueueFeature]:
        return _FEATURES

    def supports(self, feature: QueueFeature, /) -> bool:
        return feature in _FEATURES

    def _key(self, lane: str, status: str) -> str:
        return f"{self._prefix}:{lane}:{status}"

    def _lane(self, lane: str | None) -> str:
        resolved = "default" if lane is None else _identifier(lane, "lane")
        if resolved not in self._lanes:
            raise ValueError("Redis logical lane is not configured")
        return resolved

    def _open(self) -> None:
        if self._closed or self._pid != os.getpid():
            raise QueueOperationError(stage="queue.redis.connection")

    def _atomic[T](self, stage: str, action: Callable[[RedisQueuePipeline, int], T]) -> T:
        self._open()
        try:
            for _ in range(self._attempts):
                pipe = self._client.pipeline(transaction=True)
                try:
                    pipe.watch(*self._keys)
                    for key in self._keys:
                        expected = "hash" if key == self._jobs else "zset"
                        if _text(pipe.type(key)) not in ("none", expected):
                            raise ValueError("Redis queue key has an incompatible type")
                    raw_time = pipe.time()
                    if (
                        not isinstance(raw_time, (tuple, list))
                        or len(raw_time) != 2
                        or any(type(part) is not int for part in raw_time)
                    ):
                        raise ValueError("Redis server time is malformed")
                    seconds, micros = raw_time
                    if seconds < 0 or not 0 <= micros < 1_000_000:
                        raise ValueError("Redis server time is invalid")
                    return action(pipe, seconds * 1_000_000 + micros)
                except self._conflicts:
                    continue
                finally:
                    pipe.reset()
        except (QueueOperationError, UnsupportedQueueFeature):
            raise
        except Exception as error:
            raise QueueOperationError(
                reason=PlatformReason.TRANSIENT_FAILURE,
                stage=f"queue.redis.{stage}",
                protected_cause=error,
            ) from None
        raise QueueOperationError(
            reason=PlatformReason.TRANSIENT_FAILURE, stage=f"queue.redis.{stage}"
        )

    def _encode(self, record: _Record) -> str:
        return json.dumps(
            {
                "job": self._codec.encode(record.job).decode(),
                "attempt": record.attempt,
                "generation": record.generation,
                "status": record.status,
                "available": record.available,
                "reserved": record.reserved,
                "expiry": record.expiry,
                "owner": record.owner,
                "token": record.token,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    def _load(self, pipe: RedisQueuePipeline, message_id: str) -> _Record | None:
        raw = pipe.hget(self._jobs, message_id)
        if raw is None:
            return None
        try:
            text = _text(raw)
            if len(text) > 2_100_000:
                raise ValueError("Redis job record exceeds size bound")
            value = json.loads(text)
            if not isinstance(value, dict) or set(value) != {
                "job",
                "attempt",
                "generation",
                "status",
                "available",
                "reserved",
                "expiry",
                "owner",
                "token",
            }:
                raise ValueError("Redis job record has invalid fields")
            if any(
                type(value[field]) is not int or value[field] < 0
                for field in ("attempt", "generation", "available", "reserved", "expiry")
            ) or value["status"] not in ("ready", "reserved", "failed"):
                raise ValueError("Redis job counters are invalid")
            job = self._codec.decode(_text(value["job"]).encode())
            if job.envelope.metadata.message_id != message_id or job.lane not in self._lanes:
                raise ValueError("Redis job identity mismatch")
            record = _Record(job=job, **{key: item for key, item in value.items() if key != "job"})
            if record.status == "reserved":
                self._reservation(record)
            elif record.owner != "" or record.token != "":
                raise ValueError("Redis inactive job retains ownership")
            return record
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE, stage="queue.redis.record"
            ) from None

    def _write(self, pipe: RedisQueuePipeline, record: _Record, *, remove: bool = False) -> None:
        message_id = record.job.envelope.metadata.message_id
        encoded = None if remove else self._encode(record)
        pipe.multi()
        for state in ("ready", "reserved", "failed"):
            pipe.zrem(self._key(record.job.lane, state), message_id)
        if encoded is None:
            pipe.hdel(self._jobs, message_id)
        else:
            pipe.hset(self._jobs, message_id, encoded)
            score = record.expiry if record.status == "reserved" else record.available
            pipe.zadd(self._key(record.job.lane, record.status), {message_id: float(score)})
        result = pipe.execute()
        if (
            not isinstance(result, list)
            or len(result) != (4 if remove else 5)
            or any(type(item) is not int for item in result)
        ):
            raise QueueOperationError(stage="queue.redis.transaction")

    def _ids(
        self, pipe: RedisQueuePipeline, lane: str, status: str, maximum: int | str, limit: int
    ) -> list[str]:
        result = pipe.zrangebyscore(self._key(lane, status), "-inf", maximum, start=0, num=limit)
        if not isinstance(result, list):
            raise QueueOperationError(stage="queue.redis.index")
        return [_identifier(_text(item), "message id") for item in result]

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt:
        if not isinstance(job, QueueJob):
            raise TypeError("Redis dispatch requires a QueueJob")
        self._lane(job.lane)
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
        self._codec.encode(job)

        def action(pipe: RedisQueuePipeline, now: int) -> QueueDispatchReceipt:
            message_id = job.envelope.metadata.message_id
            if self._load(pipe, message_id) is not None:
                raise QueueOperationError(
                    reason=PlatformReason.INTEGRITY_FAILURE, stage="queue.redis.duplicate"
                )
            available = _micros(_date(now) + (job.delay or timedelta(0)))
            self._write(pipe, _Record(job=job, available=available))
            return QueueDispatchReceipt(
                message_id, job.lane, QueueDispatchDisposition.ENQUEUED, _date(now)
            )

        return self._atomic("dispatch", action)

    def _reservation(self, record: _Record) -> QueueReservation:
        return QueueReservation(
            job=record.job,
            attempt=record.attempt,
            reserved_at=_date(record.reserved),
            expires_at=_date(record.expiry),
            fencing_token=record.generation,
            owner_id=record.owner,
            reservation_id=record.token,
        )

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
        resolved = self._lane(lane)

        def action(pipe: RedisQueuePipeline, now: int) -> QueueReservation | None:
            ids = self._ids(pipe, resolved, "reserved", now, 1) or self._ids(
                pipe, resolved, "ready", now, 1
            )
            if not ids:
                return None
            record = self._load(pipe, ids[0])
            if (
                record is None
                or record.job.lane != resolved
                or record.status not in ("ready", "reserved")
            ):
                raise QueueOperationError(
                    reason=PlatformReason.INTEGRITY_FAILURE, stage="queue.redis.index"
                )
            if (record.status == "reserved" and record.expiry > now) or (
                record.status == "ready" and record.available > now
            ):
                raise QueueOperationError(
                    reason=PlatformReason.INTEGRITY_FAILURE, stage="queue.redis.index"
                )
            if record.attempt >= record.job.max_attempts:
                self._write(
                    pipe, replace(record, status="failed", available=now, owner="", token="")
                )
                return None
            claimed = replace(
                record,
                status="reserved",
                attempt=record.attempt + 1,
                generation=record.generation + 1,
                reserved=now,
                expiry=_micros(_date(now) + self._lease),
                owner=self._owner,
                token=secrets.token_hex(32),
            )
            self._write(pipe, claimed)
            return self._reservation(claimed)

        return self._atomic("reserve", action)

    def _owned(self, pipe: RedisQueuePipeline, now: int, reservation: QueueReservation) -> _Record:
        record = self._load(pipe, reservation.job.envelope.metadata.message_id)
        if (
            record is None
            or record.status != "reserved"
            or record.expiry <= now
            or record.owner != self._owner
            or self._reservation(record)
            != replace(reservation, connection_name=None, connection_binding=None)
        ):
            raise QueueReservationLost(stage="queue.redis.settle")
        return record

    def _settle(
        self, reservation: QueueReservation, kind: str, available_at: datetime | None = None
    ) -> QueueReservation | None:
        if not isinstance(reservation, QueueReservation):
            raise TypeError("Redis settlement requires a QueueReservation")
        when = None if available_at is None else _micros(_aware_utc(available_at, "retry time"))

        def action(pipe: RedisQueuePipeline, now: int) -> QueueReservation | None:
            record = self._owned(pipe, now, reservation)
            if kind == "ack":
                self._write(pipe, record, remove=True)
            elif kind == "renew":
                expiry = _micros(_date(now) + self._lease)
                if expiry <= record.expiry:
                    raise QueueReservationLost(stage="queue.redis.renew")
                renewed = replace(record, expiry=expiry, token=secrets.token_hex(32))
                self._write(pipe, renewed)
                return self._reservation(renewed)
            else:
                failed = kind == "reject" or (
                    kind == "retry" and record.attempt >= record.job.max_attempts
                )
                available = (
                    now
                    if kind != "retry" or failed
                    else (
                        when
                        if when is not None
                        else _micros(_date(now) + (record.job.backoff or timedelta(0)))
                    )
                )
                self._write(
                    pipe,
                    replace(
                        record,
                        status="failed" if failed else "ready",
                        available=max(now, available),
                        owner="",
                        token="",
                        attempt=record.attempt - 1 if kind == "release" else record.attempt,
                    ),
                )
            return None

        return self._atomic(kind, action)

    def ack(self, reservation: QueueReservation, /) -> None:
        self._settle(reservation, "ack")

    def reject(self, reservation: QueueReservation, /) -> None:
        self._settle(reservation, "reject")

    def release(self, reservation: QueueReservation, /) -> None:
        self._settle(reservation, "release")

    def retry(
        self, reservation: QueueReservation, /, *, available_at: datetime | None = None
    ) -> None:
        self._settle(reservation, "retry", available_at)

    def renew(self, reservation: QueueReservation, /) -> QueueReservation:
        result = self._settle(reservation, "renew")
        assert result is not None
        return result

    def dead_letters(
        self, *, lane: str | None = None, limit: int = 100
    ) -> tuple[QueueDeadLetter, ...]:
        resolved = self._lane(lane)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Redis failed job limit must be within 1..1000")

        def action(pipe: RedisQueuePipeline, now: int) -> tuple[QueueDeadLetter, ...]:
            letters = []
            for message_id in self._ids(pipe, resolved, "failed", "+inf", limit):
                record = self._load(pipe, message_id)
                if record is None or record.status != "failed" or record.job.lane != resolved:
                    raise QueueOperationError(
                        reason=PlatformReason.INTEGRITY_FAILURE, stage="queue.redis.index"
                    )
                letters.append(QueueDeadLetter(record.job, record.attempt, _date(record.available)))
            # Validate the read snapshot against concurrent replays/settlements.
            pipe.multi()
            pipe.execute()
            return tuple(letters)

        return self._atomic("dead-letters", action)

    def replay_dead_letter(self, message_id: str, /) -> QueueDeadLetterReplayReceipt:
        _identifier(message_id, "message id")

        def action(pipe: RedisQueuePipeline, now: int) -> QueueDeadLetterReplayReceipt:
            record = self._load(pipe, message_id)
            if record is None or record.status != "failed":
                raise QueueOperationError(stage="queue.redis.replay")
            self._write(
                pipe, replace(record, status="ready", attempt=0, available=now, owner="", token="")
            )
            return QueueDeadLetterReplayReceipt(message_id, record.attempt, _date(now))

        return self._atomic("replay", action)

    def close(self) -> None:
        if self._closed:
            return
        self._open()
        try:
            self._client.close()
        except Exception as error:
            raise QueueOperationError(stage="queue.redis.close", protected_cause=error) from None
        self._closed = True
