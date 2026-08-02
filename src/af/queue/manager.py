"""Named queue driver manager with verified capability routing."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    Manager,
    ManagerConfigurationError,
)
from af.framework.unit_of_work import UnitOfWork
from af.observability.signals import TelemetryPublisher
from af.queue.contracts import QueueDispatchReceipt, QueueJob, QueueReservation

__all__ = ("QueueDriver", "QueueFeature", "QueueManager", "bind_queue_manager")


class QueueFeature(StrEnum):
    """Capabilities independently verified before queue operations."""

    INLINE_EXECUTION = "inline-execution"
    RESERVATION = "reservation"
    DELAY = "delay"
    PRIORITY = "priority"
    RETRY = "retry"
    TIMEOUT = "timeout"
    UNIQUENESS = "uniqueness"
    FIFO = "fifo"
    TRANSACTIONAL_DISPATCH = "transactional-dispatch"


class QueueDriver(Protocol):
    """Minimum synchronous backend contract owned by QueueManager."""

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt: ...

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None: ...

    def ack(self, reservation: QueueReservation, /) -> None: ...

    def reject(self, reservation: QueueReservation, /) -> None: ...

    def retry(
        self,
        reservation: QueueReservation,
        /,
        *,
        available_at: datetime | None = None,
    ) -> None: ...

    def close(self) -> None: ...


def _capabilities(features: CapabilitySet | Iterable[str | QueueFeature]) -> CapabilitySet:
    if isinstance(features, CapabilitySet):
        return features
    if isinstance(features, str):
        return CapabilitySet((features,))
    values: list[str] = []
    for feature in tuple(features):
        if isinstance(feature, QueueFeature):
            values.append(feature.value)
        elif type(feature) is str:
            values.append(feature)
        else:
            raise ValueError("queue requirements must be capability identifiers")
    return CapabilitySet(values)


def _dispatch_capabilities(job: QueueJob) -> CapabilitySet:
    if not isinstance(job, QueueJob):
        raise TypeError("queue dispatch requires a QueueJob")
    required: list[QueueFeature] = []
    if job.delay is not None:
        required.append(QueueFeature.DELAY)
    if job.priority is not None:
        required.append(QueueFeature.PRIORITY)
    if job.max_attempts > 1 or job.backoff is not None:
        required.append(QueueFeature.RETRY)
    if job.timeout is not None:
        required.append(QueueFeature.TIMEOUT)
    if job.unique_key is not None:
        required.append(QueueFeature.UNIQUENESS)
    if job.fifo_group is not None:
        required.append(QueueFeature.FIFO)
    if job.requires_transaction:
        required.append(QueueFeature.TRANSACTIONAL_DISPATCH)
    return _capabilities(required)


class QueueManager(Manager[QueueDriver]):
    """Application-owned named queue connections."""

    def __init__(
        self,
        *,
        connections: Mapping[str, object],
        default: str | None,
        deployment_profile: str,
        policy_version: str,
        clock: Callable[[], datetime] | None = None,
        telemetry: TelemetryPublisher | None = None,
    ) -> None:
        super().__init__(
            manager_id="queue",
            connections=connections,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )

    def install(self, registration: DriverRegistration[QueueDriver]) -> None:
        """Install one inert queue registration."""

        if type(registration) is not DriverRegistration or registration.manager_id != "queue":
            raise ManagerConfigurationError(
                protected_cause=ValueError("driver registration does not target the queue manager")
            )
        self.extend(
            registration.driver_name,
            registration.factory,
            manifest=registration.manifest,
            verifier=registration.verifier,
            disposer=registration.disposer,
        )

    def connection(
        self,
        name: str | None = None,
        *,
        requires: CapabilitySet | Iterable[str | QueueFeature] = (),
    ) -> QueueDriver:
        return super().connection(name, requires=_capabilities(requires))

    def dispatch(self, job: QueueJob, *, name: str | None = None) -> QueueDispatchReceipt:
        return self.connection(name, requires=_dispatch_capabilities(job)).dispatch(job)

    def dispatch_after_commit(
        self,
        unit: UnitOfWork,
        job: QueueJob,
        *,
        name: str | None = None,
    ) -> None:
        if not isinstance(unit, UnitOfWork):
            raise TypeError("queue after-commit dispatch requires a UnitOfWork")
        if not isinstance(job, QueueJob):
            raise TypeError("queue after-commit dispatch requires a QueueJob")
        unit.after_commit(lambda: self.dispatch(job, name=name))

    def reserve(
        self,
        *,
        lane: str | None = None,
        name: str | None = None,
    ) -> QueueReservation | None:
        return self.connection(name, requires=(QueueFeature.RESERVATION,)).reserve(lane=lane)

    def ack(self, reservation: QueueReservation, *, name: str | None = None) -> None:
        self.connection(name, requires=(QueueFeature.RESERVATION,)).ack(reservation)

    def reject(self, reservation: QueueReservation, *, name: str | None = None) -> None:
        self.connection(name, requires=(QueueFeature.RESERVATION,)).reject(reservation)

    def retry(
        self,
        reservation: QueueReservation,
        *,
        available_at: datetime | None = None,
        name: str | None = None,
    ) -> None:
        self.connection(name, requires=(QueueFeature.RESERVATION, QueueFeature.RETRY)).retry(
            reservation,
            available_at=available_at,
        )


def bind_queue_manager(
    registrar: ProviderRegistrar,
    *,
    connections: Mapping[str, object],
    default: str | None,
    deployment_profile: str,
    policy_version: str,
    registrations: Iterable[DriverRegistration[QueueDriver]],
    clock: Callable[[], datetime] | None = None,
    telemetry: TelemetryPublisher | None = None,
) -> None:
    """Bind one application-owned QueueManager from inert registrations."""

    snapshot = tuple(registrations)
    if (
        not snapshot
        or any(
            type(registration) is not DriverRegistration or registration.manager_id != "queue"
            for registration in snapshot
        )
        or len({registration.driver_name for registration in snapshot}) != len(snapshot)
    ):
        raise ManagerConfigurationError(
            protected_cause=ValueError(
                "queue registrations must be non-empty, unique, and targeted"
            )
        )

    template = QueueManager(
        connections=connections,
        default=default,
        deployment_profile=deployment_profile,
        policy_version=policy_version,
        clock=clock,
    )
    connection_snapshot: Mapping[str, object] = MappingProxyType(
        {name: definition[1] for name, definition in template._definitions.items()}
    )
    template.close()

    def create_manager(_resolver: Resolver) -> QueueManager:
        manager = QueueManager(
            connections=connection_snapshot,
            default=default,
            deployment_profile=deployment_profile,
            policy_version=policy_version,
            clock=clock,
            telemetry=telemetry,
        )
        for registration in snapshot:
            manager.install(registration)
        return manager

    registrar.bind(
        QueueManager,
        create_manager,
        scope=Scope.APPLICATION,
        owned=True,
        replace=False,
    )
