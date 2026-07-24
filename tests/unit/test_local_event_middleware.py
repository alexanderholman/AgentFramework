from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from af.messaging import (
    AsyncLocalEventCallNext,
    EnvelopeKind,
    InvalidMessageSchema,
    LocalEventBus,
    LocalEventCallNext,
    LocalEventDispatchReport,
    LocalEventListenerError,
    LocalEventMiddleware,
    LocalEventMiddlewareError,
    LocalEventTelemetryMiddleware,
    MessageEnvelope,
    MessageMetadata,
    MessageSchema,
    MessageSchemaRegistry,
)
from af.observability import (
    TelemetryContext,
    TelemetryPublisher,
    TelemetrySignal,
)

_NOW = datetime(2026, 7, 24, 4, 0, tzinfo=UTC)


def _registry() -> MessageSchemaRegistry:
    registry = MessageSchemaRegistry()
    registry.register_schema(
        MessageSchema(
            message_type="tasks.changed",
            kind=EnvelopeKind.DOMAIN_EVENT,
            current_version=1,
        )
    )
    return registry


def _envelope(*, telemetry: TelemetryContext | None = None) -> MessageEnvelope:
    correlation_id = None if telemetry is None else telemetry.correlation_id
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id="message-1",
            message_type="tasks.changed",
            schema_version=1,
            kind=EnvelopeKind.DOMAIN_EVENT,
            occurred_at=_NOW,
            correlation_id=correlation_id,
            telemetry=telemetry,
        ),
        payload={"private": "payload"},
    )


class _RecordingMiddleware:
    def __init__(self, name: str, observed: list[str]) -> None:
        self._name = name
        self._observed = observed

    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        assert envelope.metadata.message_type == "tasks.changed"
        self._observed.append(f"{self._name}:before-sync")
        report = call_next()
        self._observed.append(f"{self._name}:after-sync")
        return report

    async def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        assert envelope.metadata.message_type == "tasks.changed"
        self._observed.append(f"{self._name}:before-async")
        report = await call_next()
        self._observed.append(f"{self._name}:after-async")
        return report


def test_local_event_middleware_wraps_sync_and_async_dispatch_in_order() -> None:
    observed: list[str] = []
    bus = LocalEventBus(
        _registry(),
        middleware=(
            _RecordingMiddleware("outer", observed),
            _RecordingMiddleware("inner", observed),
        ),
    )

    def listener(envelope: MessageEnvelope) -> None:
        assert envelope.payload_copy() == {"private": "payload"}
        observed.append("listener-sync")

    bus.subscribe("tasks.changed", listener)
    bus.freeze()

    report = bus.publish(_envelope())

    assert report.listener_count == 1
    assert observed == [
        "outer:before-sync",
        "inner:before-sync",
        "listener-sync",
        "inner:after-sync",
        "outer:after-sync",
    ]

    observed.clear()

    async def async_listener(envelope: MessageEnvelope) -> None:
        assert envelope.metadata.schema_version == 1
        observed.append("listener-async")

    async_bus = LocalEventBus(
        _registry(),
        middleware=(
            _RecordingMiddleware("outer", observed),
            _RecordingMiddleware("inner", observed),
        ),
    )
    async_bus.subscribe("tasks.changed", async_listener)
    async_bus.freeze()

    async_report = asyncio.run(async_bus.publish_async(_envelope()))

    assert async_report.listener_count == 1
    assert observed == [
        "outer:before-async",
        "inner:before-async",
        "listener-async",
        "inner:after-async",
        "outer:after-async",
    ]


class _SkipMiddleware:
    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del call_next
        return LocalEventDispatchReport(
            message_type=envelope.metadata.message_type,
            schema_version=envelope.metadata.schema_version,
            listener_count=0,
        )

    async def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del call_next
        return LocalEventDispatchReport(
            message_type=envelope.metadata.message_type,
            schema_version=envelope.metadata.schema_version,
            listener_count=0,
        )


class _ReplaceEvidenceMiddleware:
    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del envelope
        report = call_next()
        return LocalEventDispatchReport(
            message_type=report.message_type,
            schema_version=report.schema_version,
            listener_count=report.listener_count + 1,
        )

    async def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del envelope
        report = await call_next()
        return LocalEventDispatchReport(
            message_type=report.message_type,
            schema_version=report.schema_version,
            listener_count=report.listener_count + 1,
        )


@pytest.mark.parametrize("middleware", [_SkipMiddleware(), _ReplaceEvidenceMiddleware()])
def test_local_event_middleware_cannot_skip_or_replace_dispatch(
    middleware: LocalEventMiddleware,
) -> None:
    bus = LocalEventBus(_registry(), middleware=(middleware,))
    bus.subscribe("tasks.changed", lambda envelope: None)
    bus.freeze()

    with pytest.raises(InvalidMessageSchema):
        bus.publish(_envelope())

    with pytest.raises(InvalidMessageSchema):
        asyncio.run(bus.publish_async(_envelope()))


class _FailingMiddleware:
    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del envelope, call_next
        raise RuntimeError("private-middleware-state")

    async def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del envelope, call_next
        raise RuntimeError("private-middleware-state")


def test_local_event_middleware_failure_is_sanitized() -> None:
    bus = LocalEventBus(_registry(), middleware=(_FailingMiddleware(),))
    bus.freeze()

    with pytest.raises(LocalEventMiddlewareError) as caught:
        bus.publish(_envelope())
    assert "private-middleware-state" not in str(caught.value)

    with pytest.raises(LocalEventMiddlewareError) as async_caught:
        asyncio.run(bus.publish_async(_envelope()))
    assert "private-middleware-state" not in str(async_caught.value)


class _CancellingMiddleware:
    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del envelope, call_next
        raise asyncio.CancelledError

    async def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del envelope, call_next
        raise asyncio.CancelledError


def test_local_event_middleware_preserves_cancellation() -> None:
    bus = LocalEventBus(_registry(), middleware=(_CancellingMiddleware(),))
    bus.freeze()

    with pytest.raises(asyncio.CancelledError):
        bus.publish(_envelope())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bus.publish_async(_envelope()))


class _Sink:
    def __init__(self) -> None:
        self.signals: list[TelemetrySignal] = []

    def emit(self, signal: TelemetrySignal, /) -> None:
        self.signals.append(signal)


def test_local_event_telemetry_propagates_safe_context_without_payload() -> None:
    failure_sink = _Sink()
    failure_publisher = TelemetryPublisher((failure_sink,))
    failure_bus = LocalEventBus(
        _registry(),
        middleware=(
            LocalEventTelemetryMiddleware(
                failure_publisher,
                clock=lambda: _NOW,
            ),
        ),
    )

    def failing(envelope: MessageEnvelope) -> None:
        del envelope
        raise RuntimeError("private-listener-state")

    failure_bus.subscribe("tasks.changed", failing)
    failure_bus.freeze()

    with pytest.raises(LocalEventListenerError):
        failure_bus.publish(_envelope())
    assert [signal.name for signal in failure_sink.signals] == [
        "messaging.local_event.started",
        "messaging.local_event.failed",
    ]

    sink = _Sink()
    publisher = TelemetryPublisher((sink,))
    telemetry = TelemetryContext(
        operation="tests.local-event",
        correlation_id="correlation-1",
        trace_id="trace-1",
        span_id="span-1",
        trace_flags=1,
    )
    bus = LocalEventBus(
        _registry(),
        middleware=(
            LocalEventTelemetryMiddleware(
                publisher,
                clock=lambda: _NOW,
            ),
        ),
    )
    bus.subscribe("tasks.changed", lambda envelope: None)
    bus.freeze()

    report = bus.publish(_envelope(telemetry=telemetry))

    assert report.listener_count == 1
    assert [signal.name for signal in sink.signals] == [
        "messaging.local_event.started",
        "messaging.local_event.completed",
    ]
    assert all(signal.context is telemetry for signal in sink.signals)
    started = dict(sink.signals[0].attributes)
    completed = dict(sink.signals[1].attributes)
    assert started == {
        "dispatch.mode": "sync",
        "message.kind": "domain-event",
        "message.schema_version": 1,
        "message.type": "tasks.changed",
        "stage": "started",
    }
    assert completed["listener.count"] == 1
    assert "message_id" not in completed
    assert "correlation_id" not in completed
    assert "payload" not in completed
    assert "private" not in repr(sink.signals)


def test_telemetry_failure_cannot_change_dispatch_and_async_outcomes_are_recorded() -> None:
    broken_publisher = TelemetryPublisher((_Sink(),))
    broken_bus = LocalEventBus(
        _registry(),
        middleware=(
            LocalEventTelemetryMiddleware(
                broken_publisher,
                clock=lambda: datetime(2026, 7, 24, 4, 0),
            ),
        ),
    )
    observed: list[str] = []
    broken_bus.subscribe("tasks.changed", lambda envelope: observed.append("called"))
    broken_bus.freeze()

    assert broken_bus.publish(_envelope()).listener_count == 1
    assert observed == ["called"]
    assert broken_publisher.evidence.accepted_signals == 0

    sink = _Sink()
    publisher = TelemetryPublisher((sink,))
    telemetry_middleware = LocalEventTelemetryMiddleware(publisher, clock=lambda: _NOW)
    bus = LocalEventBus(_registry(), middleware=(telemetry_middleware,))

    async def cancelled(envelope: MessageEnvelope) -> None:
        del envelope
        raise asyncio.CancelledError

    bus.subscribe("tasks.changed", cancelled)
    bus.freeze()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bus.publish_async(_envelope()))
    assert [signal.name for signal in sink.signals] == [
        "messaging.local_event.started",
        "messaging.local_event.cancelled",
    ]
    assert all(dict(signal.attributes)["dispatch.mode"] == "async" for signal in sink.signals)


class _CountingMiddleware:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del envelope
        self.calls += 1
        return call_next()

    async def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        del envelope
        self.calls += 1
        return await call_next()


def test_sync_async_listener_preflight_runs_before_middleware_side_effects() -> None:
    middleware = _CountingMiddleware()
    bus = LocalEventBus(_registry(), middleware=(middleware,))

    async def listener(envelope: MessageEnvelope) -> None:
        del envelope

    bus.subscribe("tasks.changed", listener)
    bus.freeze()

    with pytest.raises(InvalidMessageSchema, match="asynchronous"):
        bus.publish(_envelope())
    assert middleware.calls == 0
