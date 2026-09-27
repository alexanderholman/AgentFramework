"""Bounded versioned JSON serialization for queue jobs on external brokers."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta
from typing import Any

from af.messaging.envelopes import JsonEnvelopeCodec
from af.queue.contracts import QueueJob, QueueJobTags
from af.queue.control_plane import WorkerJobCoordinates

__all__ = ("QueueJobCodec",)

_FIELDS = frozenset(
    {
        "version",
        "envelope",
        "lane",
        "delay",
        "priority",
        "max_attempts",
        "backoff",
        "timeout",
        "unique_key",
        "fifo_group",
        "requires_transaction",
        "tags",
    }
)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("queue wire object contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("queue wire numbers must be finite")


def _microseconds(value: timedelta | None) -> int | None:
    if value is None:
        return None
    return (value.days * 86400 + value.seconds) * 1_000_000 + value.microseconds


def _duration(value: object) -> timedelta | None:
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError("queue wire duration must be positive integer microseconds")
    try:
        return timedelta(microseconds=value)
    except OverflowError:
        raise ValueError("queue wire duration is out of range") from None


class QueueJobCodec:
    """Transport only validated AF values; never deserialize executable objects."""

    def __init__(self, *, max_bytes: int = 1_048_576) -> None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("queue codec byte bound must be positive")
        self._max_bytes = max_bytes
        self._envelopes = JsonEnvelopeCodec()

    def encode(self, job: QueueJob, /) -> bytes:
        if not isinstance(job, QueueJob):
            raise TypeError("queue codec requires a QueueJob")
        payload = {
            "version": 1,
            "envelope": json.loads(self._envelopes.encode(job.envelope)),
            "lane": job.lane,
            "delay": _microseconds(job.delay),
            "priority": job.priority,
            "max_attempts": job.max_attempts,
            "backoff": _microseconds(job.backoff),
            "timeout": _microseconds(job.timeout),
            "unique_key": job.unique_key,
            "fifo_group": job.fifo_group,
            "requires_transaction": job.requires_transaction,
            "tags": None if job.tags is None else asdict(job.tags),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._bounded(encoded)
        return encoded

    def decode(self, encoded: bytes, /) -> QueueJob:
        self._bounded(encoded)
        try:
            payload: Any = json.loads(
                encoded, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            if type(payload) is not dict or set(payload) != _FIELDS:
                raise ValueError("queue wire fields are invalid")
            if type(payload["version"]) is not int or payload["version"] != 1:
                raise ValueError("queue wire version is invalid")
            tags = payload["tags"]
            if tags is not None:
                if type(tags) is not dict:
                    raise ValueError("queue wire tags are invalid")
                coordinates = tags.pop("coordinates", None)
                if coordinates is not None:
                    if type(coordinates) is not dict:
                        raise ValueError("queue wire coordinates are invalid")
                    tags["coordinates"] = WorkerJobCoordinates(**coordinates)
                tags = QueueJobTags(**tags)
            return QueueJob(
                envelope=self._envelopes.decode(json.dumps(payload["envelope"]).encode("utf-8")),
                lane=payload["lane"],
                delay=_duration(payload["delay"]),
                priority=payload["priority"],
                max_attempts=payload["max_attempts"],
                backoff=_duration(payload["backoff"]),
                timeout=_duration(payload["timeout"]),
                unique_key=payload["unique_key"],
                fifo_group=payload["fifo_group"],
                requires_transaction=payload["requires_transaction"],
                tags=tags,
            )
        except (TypeError, ValueError, OverflowError, RecursionError) as error:
            raise ValueError("queue wire job is invalid") from error

    def _bounded(self, encoded: object) -> None:
        if type(encoded) is not bytes or not 0 < len(encoded) <= self._max_bytes:
            raise ValueError("queue wire job must be bounded non-empty bytes")
