"""Named queue driver manager with verified capability routing."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.errors import PlatformReason
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    Manager,
    ManagerConfigurationError,
)
from af.framework.unit_of_work import UnitOfWork
from af.observability.signals import TelemetryPublisher
from af.queue.contracts import (
    QueueDeadLetter,
    QueueDeadLetterReplayReceipt,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
    QueueReservationLost,
)

__all__ = ("QueueDriver", "QueueFeature", "QueueManager", "bind_queue_manager")


class QueueFeature(StrEnum):
    """Capabilities independently verified before queue operations."""

    INLINE_EXECUTION = "inline-execution"
    RESERVATION = "reservation"
    RESERVATION_RENEWAL = "reservation-renewal"
    DELAY = "delay"
    PRIORITY = "priority"
    RETRY = "retry"
    DEAD_LETTER_INSPECTION = "dead-letter-inspection"
    DEAD_LETTER_REPLAY = "dead-letter-replay"
    TIMEOUT = "timeout"
    UNIQUENESS = "uniqueness"
    FIFO = "fifo"
    TRANSACTIONAL_DISPATCH = "transactional-dispatch"


class QueueDriver(Protocol):
    """Minimum synchronous backend contract owned by QueueManager.

    Every operation, including ``close``, is an operationally bounded trust boundary;
    callers cannot safely preempt an implementation blocked inside a synchronous call.
    """

    def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt: ...

    def reserve(self, *, lane: str | None = None) -> QueueReservation | None: ...

    def ack(self, reservation: QueueReservation, /) -> None: ...

    def reject(self, reservation: QueueReservation, /) -> None: ...

    def release(self, reservation: QueueReservation, /) -> None: ...

    def retry(
        self,
        reservation: QueueReservation,
        /,
        *,
        available_at: datetime | None = None,
    ) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class _QueueRenewalDriver(Protocol):
    def renew(self, reservation: QueueReservation, /) -> QueueReservation: ...


@runtime_checkable
class _QueueDeadLetterInspectionDriver(Protocol):
    def dead_letters(
        self,
        *,
        lane: str | None = None,
        limit: int = 100,
    ) -> tuple[QueueDeadLetter, ...]: ...


@runtime_checkable
class _QueueDeadLetterReplayDriver(Protocol):
    def replay_dead_letter(self, message_id: str, /) -> QueueDeadLetterReplayReceipt: ...


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
        self._reservation_route_secret = secrets.token_bytes(32)

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
        connection_name = self._requested_name(name)
        reservation = self.connection(
            connection_name,
            requires=(QueueFeature.RESERVATION,),
        ).reserve(lane=lane)
        if reservation is None:
            return None
        if reservation.reservation_id is None:
            return reservation
        return replace(
            reservation,
            connection_name=connection_name,
            connection_binding=self._route_binding(
                connection_name,
                reservation.reservation_id,
            ),
        )

    def ack(self, reservation: QueueReservation, *, name: str | None = None) -> None:
        resolved = self._settlement_connection(reservation, name=name, stage="queue.ack")
        self.connection(resolved, requires=(QueueFeature.RESERVATION,)).ack(reservation)

    def reject(self, reservation: QueueReservation, *, name: str | None = None) -> None:
        resolved = self._settlement_connection(reservation, name=name, stage="queue.reject")
        self.connection(resolved, requires=(QueueFeature.RESERVATION,)).reject(reservation)

    def release(self, reservation: QueueReservation, *, name: str | None = None) -> None:
        """Release an interrupted reservation without consuming an attempt."""
        resolved = self._settlement_connection(reservation, name=name, stage="queue.release")
        self.connection(resolved, requires=(QueueFeature.RESERVATION,)).release(reservation)

    def renew(
        self,
        reservation: QueueReservation,
        *,
        name: str | None = None,
    ) -> QueueReservation:
        """Extend an owned lease and return its replacement settlement handle."""

        resolved = self._settlement_connection(reservation, name=name, stage="queue.renew")
        driver = self.connection(
            resolved,
            requires=(QueueFeature.RESERVATION, QueueFeature.RESERVATION_RENEWAL),
        )
        if not isinstance(driver, _QueueRenewalDriver):
            raise QueueReservationLost(stage="queue.renew")
        renewed = driver.renew(reservation)
        if not self._valid_renewal(reservation, renewed):
            raise QueueReservationLost(stage="queue.renew")
        if resolved is None:
            return renewed
        assert renewed.reservation_id is not None
        return replace(
            renewed,
            connection_name=resolved,
            connection_binding=self._route_binding(resolved, renewed.reservation_id),
        )

    def retry(
        self,
        reservation: QueueReservation,
        *,
        available_at: datetime | None = None,
        name: str | None = None,
    ) -> None:
        resolved = self._settlement_connection(reservation, name=name, stage="queue.retry")
        self.connection(
            resolved,
            requires=(QueueFeature.RESERVATION, QueueFeature.RETRY),
        ).retry(
            reservation,
            available_at=available_at,
        )

    def dead_letters(
        self,
        *,
        lane: str | None = None,
        limit: int = 100,
        name: str | None = None,
    ) -> tuple[QueueDeadLetter, ...]:
        """Inspect terminal deliveries through a capability-verified connection."""

        driver = self.connection(name, requires=(QueueFeature.DEAD_LETTER_INSPECTION,))
        if not isinstance(driver, _QueueDeadLetterInspectionDriver):
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="queue.dead-letter.inspect",
            )
        result = driver.dead_letters(lane=lane, limit=limit)
        if type(result) is not tuple or any(
            not isinstance(dead_letter, QueueDeadLetter) for dead_letter in result
        ):
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="queue.dead-letter.inspect",
            )
        return result

    def replay_dead_letter(
        self,
        message_id: str,
        /,
        *,
        name: str | None = None,
    ) -> QueueDeadLetterReplayReceipt:
        """Replay one terminal delivery through a capability-verified connection."""

        driver = self.connection(name, requires=(QueueFeature.DEAD_LETTER_REPLAY,))
        if not isinstance(driver, _QueueDeadLetterReplayDriver):
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="queue.dead-letter.replay",
            )
        receipt = driver.replay_dead_letter(message_id)
        if not isinstance(receipt, QueueDeadLetterReplayReceipt):
            raise QueueOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="queue.dead-letter.replay",
            )
        return receipt

    def _settlement_connection(
        self,
        reservation: QueueReservation,
        *,
        name: str | None,
        stage: str,
    ) -> str | None:
        if not isinstance(reservation, QueueReservation):
            raise TypeError("queue settlement requires a QueueReservation")
        bound = reservation.connection_name
        if bound is None:
            if reservation.reservation_id is not None:
                raise QueueReservationLost(stage=stage)
            return name
        if reservation.connection_binding is None or reservation.reservation_id is None:
            raise QueueReservationLost(stage=stage)
        expected = self._route_binding(bound, reservation.reservation_id)
        if not hmac.compare_digest(reservation.connection_binding, expected):
            raise QueueReservationLost(stage=stage)
        if name is None:
            return bound
        requested = self._requested_name(name)
        if requested != bound:
            raise QueueReservationLost(stage=stage)
        return bound

    def _route_binding(self, connection_name: str, reservation_id: str) -> str:
        payload = f"{connection_name}\0{reservation_id}".encode()
        return hmac.digest(self._reservation_route_secret, payload, "sha256").hex()

    @staticmethod
    def _valid_renewal(original: QueueReservation, renewed: object) -> bool:
        return (
            isinstance(renewed, QueueReservation)
            and original.expires_at is not None
            and renewed.expires_at is not None
            and renewed.expires_at > original.expires_at
            and original.reservation_id is not None
            and renewed.reservation_id is not None
            and renewed.reservation_id != original.reservation_id
            and renewed.job == original.job
            and renewed.attempt == original.attempt
            and renewed.reserved_at == original.reserved_at
            and renewed.fencing_token == original.fencing_token
            and renewed.owner_id == original.owner_id
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
