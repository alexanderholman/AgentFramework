"""Beanstalkd registration that requires independent deployment capability evidence."""

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
from af.queue.beanstalkd import _FEATURES, BeanstalkdClient, BeanstalkdQueueDriver
from af.queue.manager import QueueDriver

__all__ = ("beanstalkd_queue_registration",)


def beanstalkd_queue_registration(
    *,
    client_factory: Callable[[], BeanstalkdClient],
    verifier: CapabilityVerifier[QueueDriver],
    clock: Callable[[], datetime] | None = None,
    empty_errors: tuple[type[Exception], ...] = (TimeoutError,),
) -> DriverRegistration[QueueDriver]:
    """Register an owned client; pass Greenstalk's TimedOutError in empty_errors.

    Configuration contains ``driver``, ``tubes`` and optional ``reservation_lease``.
    The client factory must configure binary bodies and finite socket timeouts.
    """
    if not callable(client_factory) or not callable(verifier):
        raise TypeError("Beanstalkd registration requires client and verifier factories")
    manifest = DriverManifest(
        driver_name="beanstalkd",
        version="0.1.0",
        artifact_digest=hashlib.sha256(
            Path(__file__).with_name("beanstalkd.py").read_bytes()
        ).hexdigest(),
        dependency_graph_digest=hashlib.sha256(b"af:beanstalkd-client-protocol:v1").hexdigest(),
        capabilities=CapabilitySet(feature.value for feature in _FEATURES),
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> QueueDriver:
        tubes, lease = config.get("tubes"), config.get("reservation_lease", 300)
        if (
            ref.manager_id != "queue"
            or ref.driver_name != "beanstalkd"
            or config.get("driver") != "beanstalkd"
            or set(config) - {"driver", "tubes", "reservation_lease"}
            or not isinstance(tubes, Mapping)
            or type(lease) is not int
            or any(type(k) is not str or type(v) is not str for k, v in tubes.items())
        ):
            raise ValueError("Beanstalkd queue connection configuration is invalid")
        client = client_factory()
        try:
            return BeanstalkdQueueDriver(
                client=client,
                tubes=tubes,
                clock=clock,
                reservation_lease=lease,
                empty_errors=empty_errors,
            )
        except Exception:
            client.close()
            raise

    return DriverRegistration(
        manager_id="queue",
        driver_name="beanstalkd",
        factory=factory,
        manifest=manifest,
        verifier=verifier,
        disposer=lambda driver: driver.close(),
    )
