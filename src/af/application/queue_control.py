"""Authorized, payload-blind failed-job control-plane operations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from af.application.bus import AuthorizationRequest, Gate, GateDecision, GateEffect
from af.framework.context import AcceptanceScope, ExecutionContext
from af.framework.errors import PlatformError, PlatformReason
from af.observability.context import TelemetryContext
from af.observability.signals import TelemetryKind, TelemetryPublisher, TelemetrySignal
from af.queue.contracts import QueueDeadLetter, QueueDeadLetterReplayReceipt

__all__ = (
    "FailedJobInspection",
    "FailedJobReplay",
    "FailedJobSummary",
    "QueueControlBackend",
    "QueueControlService",
)


@runtime_checkable
class QueueControlBackend(Protocol):
    """Capability-routed failed-job operations required from a queue manager."""

    def dead_letters(
        self,
        *,
        lane: str | None = None,
        limit: int = 100,
        name: str | None = None,
    ) -> tuple[QueueDeadLetter, ...]: ...

    def replay_dead_letter(
        self,
        message_id: str,
        /,
        *,
        name: str | None = None,
    ) -> QueueDeadLetterReplayReceipt: ...


@dataclass(frozen=True, slots=True, repr=False)
class FailedJobSummary:
    """Authorized operator projection with no envelope, payload, or tag values."""

    message_id: str
    lane: str
    attempts: int
    dead_lettered_at: datetime

    def __repr__(self) -> str:
        return (
            "FailedJobSummary(message_id='[REDACTED]', lane='[REDACTED]', "
            f"attempts={self.attempts!r}, "
            f"dead_lettered_at={self.dead_lettered_at.isoformat()!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FailedJobInspection:
    """Bounded inspection results plus the decision that authorized access."""

    items: tuple[FailedJobSummary, ...]
    authorization: GateDecision

    def __repr__(self) -> str:
        return (
            f"FailedJobInspection(count={len(self.items)!r}, authorization={self.authorization!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class FailedJobReplay:
    """Payload-blind replay evidence plus its authorization decision."""

    message_id: str
    previous_attempts: int
    replayed_at: datetime
    authorization: GateDecision

    def __repr__(self) -> str:
        return (
            "FailedJobReplay(message_id='[REDACTED]', "
            f"previous_attempts={self.previous_attempts!r}, "
            f"replayed_at={self.replayed_at.isoformat()!r}, "
            f"authorization={self.authorization!r})"
        )


def _count_bucket(count: int) -> str:
    if count == 0:
        return "none"
    if count == 1:
        return "one"
    return "multiple"


class QueueControlService:
    """Authorize failed-job inspection and replay before protected queue access."""

    def __init__(
        self,
        backend: QueueControlBackend,
        *,
        gate: Gate,
        telemetry: TelemetryPublisher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(backend, QueueControlBackend):
            raise TypeError("queue control backend is invalid")
        if not isinstance(gate, Gate):
            raise TypeError("queue control authorization gate is invalid")
        if telemetry is not None and not isinstance(telemetry, TelemetryPublisher):
            raise TypeError("queue control telemetry publisher is invalid")
        if not callable(clock):
            raise TypeError("queue control clock must be callable")
        self._backend = backend
        self._gate = gate
        self._telemetry = telemetry
        self._clock = clock

    def inspect(
        self,
        *,
        context: ExecutionContext,
        lane: str | None = None,
        limit: int = 100,
        name: str | None = None,
    ) -> FailedJobInspection:
        """Return a payload-blind bounded projection after explicit authorization."""

        authorization = self._authorize(
            context,
            action="inspect",
            protected_resource_id=f"{name or 'default'}:{lane or '*'}",
        )
        try:
            dead_letters = self._backend.dead_letters(lane=lane, limit=limit, name=name)
            if type(dead_letters) is not tuple or any(
                not isinstance(dead_letter, QueueDeadLetter) for dead_letter in dead_letters
            ):
                raise PlatformError(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="queue.control.inspect",
                )
            items = tuple(
                FailedJobSummary(
                    message_id=dead_letter.message_id,
                    lane=dead_letter.lane,
                    attempts=dead_letter.attempts,
                    dead_lettered_at=dead_letter.dead_lettered_at,
                )
                for dead_letter in dead_letters
            )
        except BaseException:
            self._publish(
                context,
                operation="queue.control.inspect",
                attributes={
                    "lane_scope": "all" if lane is None else "filtered",
                    "status": "failed",
                },
            )
            raise
        self._publish(
            context,
            operation="queue.control.inspect",
            attributes={
                "count": _count_bucket(len(items)),
                "lane_scope": "all" if lane is None else "filtered",
                "status": "succeeded",
            },
        )
        return FailedJobInspection(items=items, authorization=authorization)

    def replay(
        self,
        message_id: str,
        /,
        *,
        context: ExecutionContext,
        name: str | None = None,
    ) -> FailedJobReplay:
        """Replay one terminal delivery after authorization and return redacted evidence."""

        authorization = self._authorize(
            context,
            action="replay",
            protected_resource_id=f"{name or 'default'}:{message_id}",
        )
        try:
            receipt = self._backend.replay_dead_letter(message_id, name=name)
            if not isinstance(receipt, QueueDeadLetterReplayReceipt):
                raise PlatformError(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="queue.control.replay",
                )
        except BaseException:
            self._publish(
                context,
                operation="queue.control.replay",
                attributes={"status": "failed"},
            )
            raise
        self._publish(
            context,
            operation="queue.control.replay",
            attributes={
                "attempts": _count_bucket(receipt.previous_attempts),
                "status": "succeeded",
            },
        )
        return FailedJobReplay(
            message_id=receipt.message_id,
            previous_attempts=receipt.previous_attempts,
            replayed_at=receipt.replayed_at,
            authorization=authorization,
        )

    def _authorize(
        self,
        context: ExecutionContext,
        *,
        action: str,
        protected_resource_id: str | None,
    ) -> GateDecision:
        decision = self._gate.decide(
            AuthorizationRequest(
                action=action,
                resource_type="queue-dead-letter",
                protected_resource_id=protected_resource_id,
                required_scope=AcceptanceScope.OPERATOR_EVALUATION,
            ),
            context,
        )
        if decision.effect is GateEffect.DENY:
            raise PlatformError(
                PlatformReason.AUTHORIZATION_DENIED,
                stage="queue.control.authorization",
                protected_cause=PermissionError("queue control access denied"),
            )
        return decision

    def _publish(
        self,
        context: ExecutionContext,
        *,
        operation: str,
        attributes: dict[str, object],
    ) -> None:
        publisher = self._telemetry
        if publisher is None:
            return
        try:
            publisher.publish(
                TelemetrySignal.create(
                    kind=TelemetryKind.LOG,
                    name=operation,
                    observed_at=self._clock(),
                    context=TelemetryContext.from_execution(
                        context,
                        operation=operation,
                    ),
                    attributes=attributes,
                )
            )
        except Exception:
            # Optional telemetry cannot change authorized queue-control semantics.
            return
