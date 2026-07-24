from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from af.messaging import (
    EnvelopeKind,
    InvalidMessageSchema,
    LocalEventBus,
    LocalEventListenerError,
    MessageEnvelope,
    MessageMetadata,
    MessageSchema,
    MessageSchemaRegistry,
)


def test_async_listener_boundary_errors_are_sanitized_and_stop_dispatch() -> None:
    registry = MessageSchemaRegistry()
    registry.register_schema(
        MessageSchema(
            message_type="tasks.changed",
            kind=EnvelopeKind.DOMAIN_EVENT,
            current_version=1,
        )
    )
    bus = LocalEventBus(registry)
    observed: list[str] = []

    async def failing(envelope: MessageEnvelope) -> None:
        del envelope
        observed.append("failing")
        raise InvalidMessageSchema("private-event-content")

    async def later(envelope: MessageEnvelope) -> None:
        del envelope
        observed.append("later")

    bus.subscribe("tasks.changed", failing)
    bus.subscribe("tasks.changed", later)
    bus.freeze()
    envelope = MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id="message-1",
            message_type="tasks.changed",
            schema_version=1,
            kind=EnvelopeKind.DOMAIN_EVENT,
            occurred_at=datetime(2026, 7, 24, 3, 0, tzinfo=UTC),
        ),
        payload={"value": 1},
    )

    with pytest.raises(LocalEventListenerError) as caught:
        asyncio.run(bus.publish_async(envelope))

    assert "private-event-content" not in str(caught.value)
    assert observed == ["failing"]
