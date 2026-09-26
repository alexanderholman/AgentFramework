"""Redis queue registration requiring independent deployment capability evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import timedelta
from pathlib import Path

from af.framework.managers import (
    CapabilitySet,
    CapabilityVerifier,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
)
from af.queue.manager import QueueDriver
from af.queue.redis import _FEATURES, RedisQueueClient, RedisQueueDriver

__all__ = ("redis_queue_registration",)


def redis_queue_registration(
    *,
    client_factory: Callable[[], RedisQueueClient],
    verifier: CapabilityVerifier[QueueDriver],
    conflict_errors: tuple[type[Exception], ...] = (),
) -> DriverRegistration[QueueDriver]:
    if not callable(client_factory) or not callable(verifier):
        raise TypeError("Redis registration requires client factory and capability verifier")
    manifest = DriverManifest(
        driver_name="redis",
        version="0.1.0",
        artifact_digest=hashlib.sha256(
            Path(__file__).with_name("redis.py").read_bytes()
        ).hexdigest(),
        dependency_graph_digest=hashlib.sha256(b"af:redis-queue-protocol:v1").hexdigest(),
        capabilities=CapabilitySet(feature.value for feature in _FEATURES),
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> QueueDriver:
        namespace, lanes = config.get("namespace"), config.get("lanes", ("default",))
        lease = config.get("reservation_lease", 300)
        if (
            ref.manager_id != "queue"
            or ref.driver_name != "redis"
            or config.get("driver") != "redis"
            or set(config) - {"driver", "namespace", "lanes", "reservation_lease"}
            or type(namespace) is not str
            or not isinstance(lanes, (tuple, list))
            or any(type(lane) is not str for lane in lanes)
            or type(lease) is not int
        ):
            raise ValueError("Redis queue connection configuration is invalid")
        client = client_factory()
        try:
            return RedisQueueDriver(
                client=client,
                namespace=namespace,
                lanes=lanes,
                reservation_lease=timedelta(seconds=lease),
                conflict_errors=conflict_errors,
            )
        except BaseException:
            client.close()
            raise

    return DriverRegistration(
        manager_id="queue",
        driver_name="redis",
        factory=factory,
        manifest=manifest,
        verifier=verifier,
        disposer=lambda driver: driver.close(),
    )
