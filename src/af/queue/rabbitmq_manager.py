"""RabbitMQ registration with deployment-supplied independent capability evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from af.framework.managers import (
    CapabilitySet,
    CapabilityVerifier,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
)
from af.queue.manager import QueueDriver
from af.queue.rabbitmq import _FEATURES, RabbitMQConnection, RabbitMQQueueDriver

__all__ = ("rabbitmq_queue_registration",)


def rabbitmq_queue_registration(
    *,
    connection_factory: Callable[[], RabbitMQConnection],
    properties_factory: Callable[..., object],
    verifier: CapabilityVerifier[QueueDriver],
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[QueueDriver]:
    if not all(callable(factory) for factory in (connection_factory, properties_factory, verifier)):
        raise TypeError(
            "RabbitMQ registration requires connection, properties and verifier factories"
        )
    manifest = DriverManifest(
        driver_name="rabbitmq",
        version="0.1.0",
        artifact_digest=hashlib.sha256(
            Path(__file__).with_name("rabbitmq.py").read_bytes()
        ).hexdigest(),
        dependency_graph_digest=hashlib.sha256(b"af:rabbitmq-client-protocol:v1").hexdigest(),
        capabilities=CapabilitySet(feature.value for feature in _FEATURES),
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> QueueDriver:
        queues, dead = config.get("queues"), config.get("dead_letter_queues")
        limit = config.get("delivery_limit", 20)
        if (
            ref.manager_id != "queue"
            or ref.driver_name != "rabbitmq"
            or config.get("driver") != "rabbitmq"
            or set(config) - {"driver", "queues", "dead_letter_queues", "delivery_limit"}
            or not isinstance(queues, Mapping)
            or not isinstance(dead, Mapping)
            or type(limit) is not int
            or any(type(k) is not str or type(v) is not str for k, v in queues.items())
            or any(type(k) is not str or type(v) is not str for k, v in dead.items())
        ):
            raise ValueError("RabbitMQ queue connection configuration is invalid")
        return RabbitMQQueueDriver(
            connection_factory=connection_factory,
            properties_factory=properties_factory,
            queues=queues,
            dead_letter_queues=dead,
            delivery_limit=limit,
            clock=clock,
        )

    return DriverRegistration(
        manager_id="queue",
        driver_name="rabbitmq",
        factory=factory,
        manifest=manifest,
        verifier=verifier,
        disposer=lambda driver: driver.close(),
    )
