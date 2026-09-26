"""Redis-py transaction protocol tests; not a substitute for a Redis service run."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import QueueJob, QueueOperationError, QueueReservationLost
from af.queue.redis import RedisQueueDriver

_NOW = datetime(2026, 9, 26, tzinfo=UTC)


def job(message_id: str = "redis-job", **options: Any) -> QueueJob:
    return QueueJob(
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW,
            ),
            payload={"work": "run"},
        ),
        **options,
    )


class Conflict(Exception):
    pass


class Client:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, dict[str, float]] = {}
        self.now = _NOW
        self.conflicts = 0
        self.closed = False
        self.fail: Exception | None = None
        self.before_execute: Any = None

    def pipeline(self, *, transaction: bool = True) -> "Pipeline":
        assert transaction
        if self.fail:
            raise self.fail
        return Pipeline(self)

    def close(self) -> None:
        self.closed = True


class Pipeline:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.commands: list[Any] = []
        self.buffer = False

    def watch(self, *keys: str) -> None:
        assert keys and all("{" in key and "}" in key for key in keys)

    def reset(self) -> None:
        pass

    def type(self, key: str) -> bytes:
        return (
            b"hash"
            if key in self.client.hashes
            else b"zset"
            if key in self.client.sets
            else b"none"
        )

    def time(self) -> tuple[int, int]:
        return int(self.client.now.timestamp()), self.client.now.microsecond

    def hget(self, key: str, field: str) -> str | None:
        return self.client.hashes.get(key, {}).get(field)

    def zrangebyscore(
        self, key: str, low: object, high: object, *, start: int, num: int
    ) -> list[bytes]:
        ordered = sorted(self.client.sets.get(key, {}).items(), key=lambda item: (item[1], item[0]))
        return [
            key.encode() for key, score in ordered if float(str(low)) <= score <= float(str(high))
        ][start : start + num]

    def multi(self) -> None:
        self.buffer = True

    def hset(self, key: str, field: str, value: str) -> None:
        assert self.buffer
        self.commands.append(
            lambda: self.client.hashes.setdefault(key, {}).__setitem__(field, value)
        )

    def hdel(self, key: str, field: str) -> None:
        assert self.buffer
        self.commands.append(lambda: self.client.hashes.setdefault(key, {}).pop(field, None))

    def zadd(self, key: str, values: dict[str, float]) -> None:
        assert self.buffer
        self.commands.append(lambda: self.client.sets.setdefault(key, {}).update(values))

    def zrem(self, key: str, member: str) -> None:
        assert self.buffer
        self.commands.append(lambda: self.client.sets.setdefault(key, {}).pop(member, None))

    def execute(self) -> list[object]:
        if self.client.before_execute:
            hook, self.client.before_execute = self.client.before_execute, None
            hook()
            raise Conflict("changed")
        if self.client.conflicts:
            self.client.conflicts -= 1
            raise Conflict("changed")
        for command in self.commands:
            command()
        return [1] * len(self.commands)


def driver(client: Client) -> RedisQueueDriver:
    return RedisQueueDriver(
        client=client,
        namespace="tests",
        lanes=("default", "critical"),
        reservation_lease=timedelta(seconds=10),
        conflict_errors=(Conflict,),
    )


def test_dispatch_restart_delay_and_exact_expiry_reclaims_with_fencing() -> None:
    client = Client()
    first = driver(client)
    first.dispatch(job(delay=timedelta(seconds=5), max_attempts=3))
    second = driver(client)
    assert second.reserve() is None
    client.now += timedelta(seconds=5)
    original = second.reserve()
    assert original and original.attempt == 1
    assert first.reserve() is None
    client.now += timedelta(seconds=10)
    recovered = first.reserve()
    assert recovered and recovered.attempt == 2
    assert recovered.fencing_token == 2
    with pytest.raises(QueueReservationLost):
        second.ack(original)
    first.ack(recovered)
    assert first.reserve() is None


def test_renew_rotates_and_release_preserves_attempt_but_not_generation() -> None:
    client = Client()
    queue = driver(client)
    queue.dispatch(job())
    original = queue.reserve()
    assert original
    client.now += timedelta(seconds=1)
    renewed = queue.renew(original)
    with pytest.raises(QueueReservationLost):
        queue.ack(original)
    with pytest.raises(QueueReservationLost):
        driver(client).ack(renewed)
    queue.release(renewed)
    again = queue.reserve()
    assert again and again.attempt == 1 and again.fencing_token == 2
    queue.ack(again)


def test_retry_deadletter_and_replay_preserve_job_and_reset_attempt() -> None:
    client = Client()
    queue = driver(client)
    value = job(max_attempts=2, backoff=timedelta(seconds=3))
    queue.dispatch(value)
    first = queue.reserve()
    assert first
    queue.retry(first)
    assert queue.reserve() is None
    client.now += timedelta(seconds=3)
    last = queue.reserve()
    assert last and last.attempt == 2
    queue.retry(last)
    failed = queue.dead_letters()
    assert len(failed) == 1 and failed[0].job == value and failed[0].attempts == 2
    receipt = queue.replay_dead_letter("redis-job")
    assert receipt.previous_attempts == 2
    replayed = queue.reserve()
    assert replayed and replayed.attempt == 1 and replayed.fencing_token == 3


def test_conflict_reloads_and_does_not_return_uncommitted_claim() -> None:
    client = Client()
    first, second = driver(client), driver(client)
    first.dispatch(job(max_attempts=2))
    acquired = []
    client.before_execute = lambda: acquired.append(second.reserve())
    assert first.reserve() is None
    assert acquired[0]
    with pytest.raises(QueueReservationLost):
        first.ack(acquired[0])
    second.ack(acquired[0])


def test_duplicates_foreign_tampering_and_ack_redispatch_aba_fail_closed() -> None:
    client = Client()
    queue = driver(client)
    queue.dispatch(job())
    with pytest.raises(QueueOperationError):
        queue.dispatch(job())
    original = queue.reserve()
    assert original
    with pytest.raises(QueueReservationLost):
        queue.ack(replace(original, job=job("other")))
    queue.ack(original)
    queue.dispatch(job())
    fresh = queue.reserve()
    assert fresh
    with pytest.raises(QueueReservationLost):
        queue.ack(original)
    queue.ack(fresh)


@pytest.mark.parametrize(
    "options",
    [
        {"lanes": "default"},
        {"lanes": ()},
        {"lanes": ("default", "default")},
        {"reservation_lease": timedelta(0)},
        {"reservation_lease": timedelta(days=2)},
        {"transaction_attempts": 0},
        {"transaction_attempts": True},
        {"conflict_errors": (object,)},
    ],
)
def test_invalid_configuration_is_rejected(options) -> None:
    with pytest.raises((TypeError, ValueError)):
        RedisQueueDriver(client=Client(), namespace="tests", **options)


@pytest.mark.parametrize(
    "options",
    [
        {"priority": 1},
        {"timeout": timedelta(seconds=1)},
        {"unique_key": "unique"},
        {"fifo_group": "group"},
        {"requires_transaction": True},
    ],
)
def test_unsupported_options_do_not_publish(options) -> None:
    from af.queue import UnsupportedQueueFeature

    client = Client()
    with pytest.raises(UnsupportedQueueFeature):
        driver(client).dispatch(job(**options))
    assert client.hashes == {}


def test_lanes_expiry_exhaustion_and_explicit_rejection() -> None:
    client = Client()
    queue = driver(client)
    queue.dispatch(job(lane="critical"))
    assert queue.reserve() is None
    claim = queue.reserve(lane="critical")
    assert claim
    client.now += timedelta(seconds=10)
    assert queue.reserve(lane="critical") is None
    failed = queue.dead_letters(lane="critical")
    assert len(failed) == 1 and failed[0].attempts == 1
    queue.replay_dead_letter("redis-job")
    again = queue.reserve(lane="critical")
    assert again
    queue.reject(again)
    assert len(queue.dead_letters(lane="critical")) == 1
    with pytest.raises(ValueError):
        queue.reserve(lane="missing")
    with pytest.raises(ValueError):
        queue.dead_letters(limit=0)
    with pytest.raises(QueueOperationError):
        queue.replay_dead_letter("missing")


def test_explicit_retry_time_and_nonadvancing_renewal() -> None:
    client = Client()
    queue = driver(client)
    queue.dispatch(job(max_attempts=2))
    claim = queue.reserve()
    assert claim
    with pytest.raises(QueueReservationLost):
        queue.renew(claim)
    queue.retry(claim, available_at=client.now + timedelta(seconds=2))
    assert queue.reserve() is None
    client.now += timedelta(seconds=2)
    assert queue.reserve()


def test_conflict_retry_exhaustion_and_backend_errors_are_sanitized() -> None:
    client = Client()
    client.conflicts = 2
    driver(client).dispatch(job())
    assert client.conflicts == 0
    client.conflicts = 100
    with pytest.raises(QueueOperationError):
        driver(client).reserve()
    client.fail = RuntimeError("secret backend address")
    with pytest.raises(QueueOperationError) as caught:
        driver(client).reserve()
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("response", [None, [1], ["1", 0], [-1, 0], [1, 1_000_000]])
def test_malformed_server_clock_is_rejected(monkeypatch, response) -> None:
    monkeypatch.setattr(Pipeline, "time", lambda self: response)
    with pytest.raises(QueueOperationError):
        driver(Client()).reserve()


@pytest.mark.parametrize("raw", ["garbage", "{}", "[]", "x" * 2_100_001, 1])
def test_corrupt_records_remain_present_without_delivery(raw) -> None:
    client = Client()
    queue = driver(client)
    queue.dispatch(job())
    records = next(iter(client.hashes.values()))
    records["redis-job"] = raw
    with pytest.raises(QueueOperationError):
        queue.reserve()
    assert records["redis-job"] == raw


@pytest.mark.parametrize(
    "field,value",
    [
        ("attempt", -1),
        ("status", "unknown"),
        ("owner", "stale"),
        ("job", "{}"),
    ],
)
def test_invalid_record_fields_fail_closed(field, value) -> None:
    import json

    client = Client()
    queue = driver(client)
    queue.dispatch(job())
    records = next(iter(client.hashes.values()))
    record = json.loads(records["redis-job"])
    record[field] = value
    records["redis-job"] = json.dumps(record)
    with pytest.raises(QueueOperationError):
        queue.reserve()


def test_record_identity_and_index_corruption_fail_closed() -> None:
    client = Client()
    queue = driver(client)
    queue.dispatch(job())
    records = next(iter(client.hashes.values()))
    records["redis-job"] = records["redis-job"].replace("redis-job", "other-job")
    with pytest.raises(QueueOperationError):
        queue.reserve()
    records.clear()
    with pytest.raises(QueueOperationError):
        queue.reserve()


def test_index_clock_and_type_inconsistency_is_not_silently_repaired(monkeypatch) -> None:
    client = Client()
    queue = driver(client)
    queue.dispatch(job(delay=timedelta(seconds=10)))
    ready = next(value for key, value in client.sets.items() if key.endswith(":ready") and value)
    ready["redis-job"] = 0
    with pytest.raises(QueueOperationError):
        queue.reserve()
    monkeypatch.setattr(Pipeline, "type", lambda self, key: b"string")
    with pytest.raises(QueueOperationError):
        queue.reserve()


@pytest.mark.parametrize(
    "method,response", [("execute", None), ("execute", [1]), ("zrangebyscore", {})]
)
def test_invalid_transaction_and_index_responses(monkeypatch, method, response) -> None:
    monkeypatch.setattr(Pipeline, method, lambda *args, **kwargs: response)
    with pytest.raises(QueueOperationError):
        if method == "execute":
            driver(Client()).dispatch(job())
        else:
            driver(Client()).reserve()


def test_corrupt_failed_index_and_lifecycle(monkeypatch) -> None:
    from af.queue import QueueFeature

    client = Client()
    queue = driver(client)
    assert queue.supports(QueueFeature.DELAY)
    assert not queue.supports(QueueFeature.PRIORITY)
    assert QueueFeature.DEAD_LETTER_REPLAY in queue.features
    with pytest.raises(TypeError):
        queue.dispatch(object())
    with pytest.raises(TypeError):
        queue.ack(object())
    queue.dispatch(job())
    claim = queue.reserve()
    assert claim
    queue.reject(claim)
    next(iter(client.hashes.values())).clear()
    with pytest.raises(QueueOperationError):
        queue.dead_letters()
    monkeypatch.setattr(client, "close", lambda: (_ for _ in ()).throw(RuntimeError("secret")))
    with pytest.raises(QueueOperationError) as caught:
        queue.close()
    assert "secret" not in str(caught.value)
    monkeypatch.undo()
    queue.close()
    queue.close()
    assert client.closed
    with pytest.raises(QueueOperationError):
        queue.reserve()


def test_registration_verified_routing_and_owned_cleanup() -> None:
    from af.framework.managers import (
        CapabilityBinding,
        CapabilityEvidence,
        CapabilitySet,
        ConnectionRef,
        HealthStatus,
    )
    from af.queue import QueueDriver, QueueManager
    from af.queue.redis_manager import redis_queue_registration

    client = Client()

    def verify(
        ref: ConnectionRef, queue: QueueDriver, binding: CapabilityBinding
    ) -> CapabilityEvidence:
        assert isinstance(queue, RedisQueueDriver)
        return CapabilityEvidence(
            binding=binding,
            capabilities=CapabilitySet(feature.value for feature in queue.features),
            health=HealthStatus.HEALTHY,
            observed_at=_NOW,
            expires_at=_NOW + timedelta(minutes=1),
            evidence_digest="a" * 64,
        )

    registration = redis_queue_registration(
        client_factory=lambda: client, verifier=verify, conflict_errors=(Conflict,)
    )
    manager = QueueManager(
        connections={"primary": {"driver": "redis", "namespace": "tests"}},
        default="primary",
        deployment_profile="test",
        policy_version="test",
        clock=lambda: _NOW,
    )
    manager.install(registration)
    manager.dispatch(job())
    claim = manager.reserve()
    assert claim
    manager.ack(claim)
    manager.close()
    assert client.closed
    with pytest.raises(ValueError):
        registration.factory(ConnectionRef("queue", "bad", "redis"), {})
    client.closed = False
    with pytest.raises(ValueError):
        registration.factory(
            ConnectionRef("queue", "bad", "redis"),
            {"driver": "redis", "namespace": "tests", "reservation_lease": 0},
        )
    assert client.closed
    with pytest.raises(TypeError):
        redis_queue_registration(client_factory=None, verifier=verify)
