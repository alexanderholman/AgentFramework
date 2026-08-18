from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from af.application.bus import Gate, GateDecision, GateEffect
from af.application.queue_control import QueueControlService
from af.framework.context import AcceptanceScope, ExecutionContext, Principal
from af.framework.errors import PlatformError, PlatformReason
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.observability.signals import TelemetryPublisher, TelemetrySignal
from af.queue.contracts import (
    QueueDeadLetter,
    QueueDeadLetterReplayReceipt,
    QueueJob,
)
from af.queue.database_manager import database_queue_registration
from af.queue.manager import QueueManager

_NOW = datetime(2026, 8, 18, 7, tzinfo=UTC)


def _dead_letter(message_id: str, *, lane: str = "critical") -> QueueDeadLetter:
    envelope = MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=message_id,
            message_type="jobs.example",
            schema_version=1,
            kind=EnvelopeKind.JOB,
            occurred_at=_NOW,
        ),
        payload={"secret": f"payload-for-{message_id}"},
    )
    return QueueDeadLetter(
        job=QueueJob(envelope=envelope, lane=lane, max_attempts=3),
        attempts=3,
        dead_lettered_at=_NOW,
    )


def _context() -> ExecutionContext:
    return ExecutionContext(
        principal=Principal(
            subject_id="operator-1",
            principal_type="user",
            roles=("operator",),
        ),
        correlation_id="control-correlation",
        requested_scope=AcceptanceScope.OPERATOR_EVALUATION,
    )


class _Backend:
    def __init__(self, dead_letters: tuple[QueueDeadLetter, ...]) -> None:
        self._dead_letters = dead_letters
        self.calls: list[tuple[object, ...]] = []

    def dead_letters(
        self,
        *,
        lane: str | None = None,
        limit: int = 100,
        name: str | None = None,
    ) -> tuple[QueueDeadLetter, ...]:
        self.calls.append(("inspect", lane, limit, name))
        return self._dead_letters[:limit]

    def replay_dead_letter(
        self,
        message_id: str,
        /,
        *,
        name: str | None = None,
    ) -> QueueDeadLetterReplayReceipt:
        self.calls.append(("replay", message_id, name))
        return QueueDeadLetterReplayReceipt(
            message_id=message_id,
            previous_attempts=3,
            replayed_at=_NOW,
        )


class _Sink:
    def __init__(self) -> None:
        self.signals: list[TelemetrySignal] = []

    def emit(self, signal: TelemetrySignal, /) -> None:
        self.signals.append(signal)


def test_queue_control_denies_before_failed_job_access_or_mutation() -> None:
    backend = _Backend((_dead_letter("job-private"),))
    service = QueueControlService(
        backend,
        gate=Gate(policy_version="policy-v1", policies=()),
    )

    with pytest.raises(PlatformError) as inspection:
        service.inspect(context=_context(), lane="critical")
    with pytest.raises(PlatformError) as replay:
        service.replay("job-private", context=_context())

    assert inspection.value.reason is PlatformReason.AUTHORIZATION_DENIED
    assert replay.value.reason is PlatformReason.AUTHORIZATION_DENIED
    assert "job-private" not in str(replay.value)
    assert backend.calls == []


def test_queue_control_projects_redacted_authorized_results_and_bounded_telemetry() -> None:
    requests: list[tuple[str, str, str | None]] = []

    def allow(request, context):  # type: ignore[no-untyped-def]
        del context
        requests.append((request.action, request.resource_type, request.protected_resource_id))
        return GateDecision(
            effect=GateEffect.ALLOW,
            policy_version="policy-v1",
            reason_code="operator_allowed",
            effective_scope=AcceptanceScope.OPERATOR_EVALUATION,
        )

    backend = _Backend(
        (
            _dead_letter("job-failed-one"),
            _dead_letter("job-failed-two"),
        )
    )
    sink = _Sink()
    service = QueueControlService(
        backend,
        gate=Gate(policy_version="policy-v1", policies=(allow,)),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    inspection = service.inspect(
        context=_context(),
        lane="critical",
        limit=10,
        name="primary",
    )
    replay = service.replay("job-failed-one", context=_context(), name="primary")

    assert tuple(item.message_id for item in inspection.items) == (
        "job-failed-one",
        "job-failed-two",
    )
    assert inspection.items[0].lane == "critical"
    assert inspection.items[0].attempts == 3
    assert inspection.items[0].dead_lettered_at == _NOW
    assert inspection.authorization.effect is GateEffect.ALLOW
    assert replay.message_id == "job-failed-one"
    assert replay.previous_attempts == 3
    assert replay.replayed_at == _NOW
    assert replay.authorization.effect is GateEffect.ALLOW
    assert backend.calls == [
        ("inspect", "critical", 10, "primary"),
        ("replay", "job-failed-one", "primary"),
    ]
    assert requests == [
        ("inspect", "queue-dead-letter", "primary:critical"),
        ("replay", "queue-dead-letter", "primary:job-failed-one"),
    ]

    rendered = repr((inspection, replay, sink.signals))
    assert "payload-for" not in rendered
    assert "operator-1" not in rendered
    assert "job-failed-one" not in rendered
    assert [signal.name for signal in sink.signals] == [
        "queue.control.inspect",
        "queue.control.replay",
    ]
    assert dict(sink.signals[0].attributes) == {
        "count": "multiple",
        "lane_scope": "filtered",
        "status": "succeeded",
    }
    assert dict(sink.signals[1].attributes) == {
        "attempts": "multiple",
        "status": "succeeded",
    }
    assert sink.signals[0].context is not None
    assert sink.signals[0].context.correlation_id == "control-correlation"


def test_queue_control_uses_verified_queue_manager_without_exposing_job_payload(
    tmp_path: Path,
) -> None:
    decision = GateDecision(
        effect=GateEffect.ALLOW,
        policy_version="policy-v1",
        reason_code="operator_allowed",
        effective_scope=AcceptanceScope.OPERATOR_EVALUATION,
    )
    manager = QueueManager(
        connections={"primary": {"driver": "database"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(tmp_path / "queue-control.sqlite3"),
            clock=lambda: _NOW,
        )
    )
    manager.dispatch(_dead_letter("job-production-control").job)
    reservation = manager.reserve(lane="critical")
    assert reservation is not None
    manager.reject(reservation)
    service = QueueControlService(
        manager,
        gate=Gate(
            policy_version="policy-v1",
            policies=(lambda _request, _context: decision,),
        ),
        clock=lambda: _NOW,
    )

    inspected = service.inspect(context=_context(), lane="critical")
    replayed = service.replay("job-production-control", context=_context())

    assert len(inspected.items) == 1
    assert inspected.items[0].message_id == "job-production-control"
    assert replayed.previous_attempts == 1
    assert "payload-for-job-production-control" not in repr((inspected, replayed))
    replacement = manager.reserve(lane="critical")
    assert replacement is not None
    assert replacement.attempt == 1
    manager.ack(replacement)
    manager.close()


def test_queue_control_reports_backend_failures_without_replacing_them() -> None:
    inspect_failure = RuntimeError("database inspection failed with private detail")
    replay_failure = OSError("database replay failed with private detail")

    class FailingBackend(_Backend):
        def dead_letters(
            self,
            *,
            lane: str | None = None,
            limit: int = 100,
            name: str | None = None,
        ) -> tuple[QueueDeadLetter, ...]:
            del lane, limit, name
            raise inspect_failure

        def replay_dead_letter(
            self,
            message_id: str,
            /,
            *,
            name: str | None = None,
        ) -> QueueDeadLetterReplayReceipt:
            del message_id, name
            raise replay_failure

    decision = GateDecision(
        effect=GateEffect.ALLOW,
        policy_version="policy-v1",
        reason_code="operator_allowed",
        effective_scope=AcceptanceScope.OPERATOR_EVALUATION,
    )
    sink = _Sink()
    service = QueueControlService(
        FailingBackend(()),
        gate=Gate(
            policy_version="policy-v1",
            policies=(lambda _request, _context: decision,),
        ),
        telemetry=TelemetryPublisher((sink,)),
        clock=lambda: _NOW,
    )

    with pytest.raises(RuntimeError) as inspected:
        service.inspect(context=_context(), lane="critical")
    with pytest.raises(OSError) as replayed:
        service.replay("job-failed", context=_context())

    assert inspected.value is inspect_failure
    assert replayed.value is replay_failure
    assert [signal.name for signal in sink.signals] == [
        "queue.control.inspect",
        "queue.control.replay",
    ]
    assert dict(sink.signals[0].attributes) == {
        "lane_scope": "filtered",
        "status": "failed",
    }
    assert dict(sink.signals[1].attributes) == {"status": "failed"}
    assert "private detail" not in repr(sink.signals)
