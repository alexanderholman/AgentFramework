"""Inert SQS manager registration with deployment-supplied capability evidence."""

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
from af.queue.sqs import _FEATURES, SQSClient, SQSQueueDriver

__all__ = ("sqs_queue_registration",)


def sqs_queue_registration(
    *,
    client_factory: Callable[[], SQSClient],
    verifier: CapabilityVerifier[QueueDriver],
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[QueueDriver]:
    """Require explicit runtime evidence rather than invent service verification.

    Connection config contains ``driver``, ``queues``, ``dead_letter_queues`` and
    optional ``visibility_timeout``. Authentication belongs to the client factory.
    """
    if not callable(client_factory) or not callable(verifier):
        raise TypeError("SQS registration requires client and verifier factories")
    manifest = DriverManifest(
        driver_name="sqs",
        version="0.1.0",
        artifact_digest=hashlib.sha256(Path(__file__).with_name("sqs.py").read_bytes()).hexdigest(),
        dependency_graph_digest=hashlib.sha256(b"af:sqs-client-protocol:v1").hexdigest(),
        capabilities=CapabilitySet(feature.value for feature in _FEATURES),
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> QueueDriver:
        allowed = {"driver", "queues", "dead_letter_queues", "visibility_timeout"}
        queues, dead = config.get("queues"), config.get("dead_letter_queues")
        visibility = config.get("visibility_timeout", 300)
        if (
            ref.manager_id != "queue"
            or ref.driver_name != "sqs"
            or config.get("driver") != "sqs"
            or set(config) - allowed
            or not isinstance(queues, Mapping)
            or not isinstance(dead, Mapping)
            or type(visibility) is not int
            or any(type(k) is not str or type(v) is not str for k, v in queues.items())
            or any(type(k) is not str or type(v) is not str for k, v in dead.items())
        ):
            raise ValueError("SQS queue connection configuration is invalid")
        client = client_factory()
        try:
            return SQSQueueDriver(
                client=client,
                queues=queues,
                dead_letter_queues=dead,
                visibility_timeout=visibility,
                clock=clock,
            )
        except Exception:
            client.close()
            raise

    return DriverRegistration(
        manager_id="queue",
        driver_name="sqs",
        factory=factory,
        manifest=manifest,
        verifier=verifier,
        disposer=lambda driver: driver.close(),
    )
