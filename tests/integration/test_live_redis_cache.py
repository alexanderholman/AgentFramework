"""Opt-in real Redis wire tests; AF_REDIS_SERVER names a local server executable."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from tempfile import TemporaryDirectory
from typing import cast

import pytest
import redis

from af.cache.contracts import CacheKey, CacheNamespace, LockOwner
from af.cache.redis import RedisCacheDriver, RedisClient


@pytest.fixture
def redis_client():
    binary = os.environ.get("AF_REDIS_SERVER")
    if binary is None:
        pytest.skip("AF_REDIS_SERVER is not configured for local Redis qualification")
    with TemporaryDirectory(prefix="af-redis-") as directory:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(
            [
                binary,
                "--port",
                str(port),
                "--bind",
                "127.0.0.1",
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                directory,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = redis.Redis(host="127.0.0.1", port=port, socket_timeout=1)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    if client.ping():
                        break
                except redis.ConnectionError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        pytest.fail("local Redis did not become ready")
                    time.sleep(0.01)
            yield client
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def test_real_redis_ttl_isolation_and_owned_fencing(redis_client):
    driver = RedisCacheDriver(client=cast(RedisClient, redis_client))
    namespace = CacheNamespace("af-test", "tenant-a", "real")
    other = CacheNamespace("af-test", "tenant-b", "real")
    key = CacheKey("value")
    driver.set(namespace, key, b"short", ttl=timedelta(milliseconds=80))
    driver.set(other, key, b"retained")
    assert driver.get(namespace, key) == b"short"
    deadline = time.monotonic() + 3
    while driver.get(namespace, key) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert driver.get(namespace, key) is None
    assert driver.get(other, key) == b"retained"
    assert driver.delete(other, key)
    with ThreadPoolExecutor(max_workers=12) as executor:
        leases = list(
            executor.map(
                lambda index: driver.acquire_lock(
                    namespace, key, LockOwner(f"owner-{index}"), ttl=timedelta(seconds=3)
                ),
                range(12),
            )
        )
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    first = winners[0]
    assert driver.release_lock(namespace, key, first)
    replacement = driver.acquire_lock(namespace, key, LockOwner("next"), ttl=timedelta(seconds=3))
    assert replacement is not None and replacement.fencing_token > first.fencing_token
    assert driver.release_lock(namespace, key, first) is False
    assert driver.renew_lock(namespace, key, first, ttl=timedelta(seconds=3)) is None
    renewed = driver.renew_lock(namespace, key, replacement, ttl=timedelta(seconds=3))
    assert renewed is not None
    assert driver.release_lock(namespace, key, renewed)
    driver.close()


def _queue_job(message_id, **options):
    from datetime import UTC, datetime

    from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
    from af.queue import QueueJob

    return QueueJob(
        envelope=MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.redis-live",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=datetime.now(UTC),
            ),
            payload={"work": "qualification"},
        ),
        **options,
    )


def _queue_driver(client, *, namespace="live-queue", lease=timedelta(seconds=2)):
    from af.queue.redis import RedisQueueClient, RedisQueueDriver

    return RedisQueueDriver(
        client=cast(RedisQueueClient, client),
        namespace=namespace,
        lanes=("default", "critical"),
        reservation_lease=lease,
        conflict_errors=(redis.WatchError,),
        transaction_attempts=100,
    )


def _eventually_reserve(driver):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        reservation = driver.reserve()
        if reservation is not None:
            return reservation
        time.sleep(0.01)
    pytest.fail("Redis queue did not make delivery available within qualification deadline")


def test_real_redis_queue_competing_claims_and_redispatch_aba(redis_client):
    from af.queue import QueueReservationLost

    drivers = [_queue_driver(redis_client) for _ in range(12)]
    drivers[0].dispatch(_queue_job("live-contention", max_attempts=2))
    with ThreadPoolExecutor(max_workers=12) as executor:
        claims = list(executor.map(lambda driver: driver.reserve(), drivers))
    winners = [(index, claim) for index, claim in enumerate(claims) if claim is not None]
    assert len(winners) == 1
    index, first = winners[0]
    with pytest.raises(QueueReservationLost):
        drivers[(index + 1) % 12].ack(first)
    drivers[index].ack(first)
    drivers[0].dispatch(_queue_job("live-contention", max_attempts=2))
    replacement = drivers[index].reserve()
    assert replacement is not None
    with pytest.raises(QueueReservationLost):
        drivers[index].ack(first)
    drivers[index].ack(replacement)
    assert drivers[0].reserve() is None


def test_real_redis_queue_delay_lease_recovery_retry_and_replay(redis_client):
    from af.queue import QueueReservationLost

    first = _queue_driver(redis_client, lease=timedelta(milliseconds=150))
    first.dispatch(
        _queue_job(
            "live-delayed",
            delay=timedelta(milliseconds=100),
            max_attempts=3,
            backoff=timedelta(milliseconds=100),
        )
    )
    assert first.reserve() is None
    original = _eventually_reserve(first)
    renewed = first.renew(original)
    assert renewed.expires_at > original.expires_at
    with pytest.raises(QueueReservationLost):
        first.ack(original)
    replacement = _queue_driver(redis_client, lease=timedelta(milliseconds=150))
    recovered = _eventually_reserve(replacement)
    assert recovered.attempt == 2 and recovered.fencing_token > original.fencing_token
    with pytest.raises(QueueReservationLost):
        first.ack(renewed)
    replacement.retry(recovered)
    assert replacement.reserve() is None
    final = _eventually_reserve(replacement)
    assert final.attempt == 3
    replacement.retry(final)
    failed = first.dead_letters()
    assert len(failed) == 1 and failed[0].message_id == "live-delayed"
    first.replay_dead_letter("live-delayed")
    replayed = first.reserve()
    assert replayed is not None and replayed.attempt == 1
    first.release(replayed)
    released = replacement.reserve()
    assert released is not None and released.attempt == 1
    replacement.ack(released)
    assert first.dead_letters() == ()
