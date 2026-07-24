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
    LocalEventTelemetryMiddleware,
    MessageEnvelope,
    MessageMetadata,
    MessageSchema,
    MessageSchemaRegistry,
)
from af.observability import TelemetryPublisher, TelemetrySignal

_NOW = datetime(2026, 7, 24, 4, 30, tzinfo=UTC)


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


def _envelope() -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id="message-1",
            message_type="tasks.changed",
            schema_version=1,
            kind=EnvelopeKind.DOMAIN_EVENT,
            occurred_at=_NOW,
        ),
        payload={"private": "payload"},
    )


class _SwallowingMiddleware:
    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        try:
            return call_next()
        except LocalEventListenerError:
            return LocalEventDispatchReport(
                message_type=envelope.metadata.message_type,
                schema_version=envelope.metadata.schema_version,
                listener_count=1,
            )

    async def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        try:
            return await call_next()
        except LocalEventListenerError:
            return LocalEventDispatchReport(
                message_type=envelope.metadata.message_type,
                schema_version=envelope.metadata.schema_version,
                listener_count=1,
            )


def test_middleware_cannot_swallow_downstream_failure_on_either_dispatch_path() -> None:
    observed: list[str] = []
    bus = LocalEventBus(_registry(), middleware=(_SwallowingMiddleware(),))

    def failing_listener(envelope: MessageEnvelope) -> None:
        del envelope
        observed.append("called")
        raise RuntimeError("private-listener-state")

    bus.subscribe("tasks.changed", failing_listener)
    bus.freeze()

    with pytest.raises(InvalidMessageSchema, match="complete next"):
        bus.publish(_envelope())
    with pytest.raises(InvalidMessageSchema, match="await next"):
        asyncio.run(bus.publish_async(_envelope()))
    assert observed == ["called", "called"]


class _Sink:
    def __init__(self) -> None:
        self.signals: list[TelemetrySignal] = []

    def emit(self, signal: TelemetrySignal, /) -> None:
        self.signals.append(signal)


def _cancelled_clock() -> datetime:
    raise asyncio.CancelledError


def test_optional_telemetry_cancelled_error_cannot_cancel_dispatch() -> None:
    sink = _Sink()
    middleware = LocalEventTelemetryMiddleware(
        TelemetryPublisher((sink,)),
        clock=_cancelled_clock,
    )
    observed: list[str] = []
    bus = LocalEventBus(_registry(), middleware=(middleware,))
    bus.subscribe("tasks.changed", lambda envelope: observed.append("sync"))
    bus.freeze()

    assert bus.publish(_envelope()).listener_count == 1
    assert observed == ["sync"]
    assert sink.signals == []

    async_observed: list[str] = []
    async_bus = LocalEventBus(_registry(), middleware=(middleware,))

    async def async_listener(envelope: MessageEnvelope) -> None:
        del envelope
        async_observed.append("async")

    async_bus.subscribe("tasks.changed", async_listener)
    async_bus.freeze()

    assert asyncio.run(async_bus.publish_async(_envelope())).listener_count == 1
    assert async_observed == ["async"]
    assert sink.signals == []
