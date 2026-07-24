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
    MessageUpcastError,
    PayloadLimits,
    UnsupportedMessageSchema,
)

_NOW = datetime(2026, 7, 24, 3, 0, tzinfo=UTC)


def _envelope(
    *,
    message_type: str = "tasks.changed",
    version: int = 1,
    kind: EnvelopeKind = EnvelopeKind.DOMAIN_EVENT,
    payload: object | None = None,
) -> MessageEnvelope:
    return MessageEnvelope.create(
        metadata=MessageMetadata(
            message_id=f"message-{version}",
            message_type=message_type,
            schema_version=version,
            kind=kind,
            occurred_at=_NOW,
        ),
        payload={"value": 1} if payload is None else payload,
    )


def _registry(
    *,
    message_type: str = "tasks.changed",
    kind: EnvelopeKind = EnvelopeKind.DOMAIN_EVENT,
    current_version: int = 1,
    minimum_version: int = 1,
) -> MessageSchemaRegistry:
    registry = MessageSchemaRegistry()
    registry.register_schema(
        MessageSchema(
            message_type=message_type,
            kind=kind,
            current_version=current_version,
            minimum_version=minimum_version,
        )
    )
    return registry


def test_registry_applies_contiguous_upcasters_once_without_mutating_source() -> None:
    source = _envelope(payload={"value": 1, "history": []})
    registry = _registry(current_version=3)
    calls: list[int] = []

    def to_v2(payload: object) -> object:
        assert isinstance(payload, dict)
        calls.append(1)
        payload["value"] = 2
        payload["history"].append("v2")  # type: ignore[union-attr]
        return payload

    def to_v3(payload: object) -> object:
        assert isinstance(payload, dict)
        calls.append(2)
        payload["value"] = 3
        payload["history"].append("v3")  # type: ignore[union-attr]
        return payload

    registry.register_upcaster("tasks.changed", 1, to_v2)
    registry.register_upcaster("tasks.changed", 2, to_v3)
    registry.freeze()

    normalized = registry.normalize(source)

    assert calls == [1, 2]
    assert source.metadata.schema_version == 1
    assert source.payload_copy() == {"history": [], "value": 1}
    assert normalized.metadata.schema_version == 3
    assert normalized.payload_copy() == {"history": ["v2", "v3"], "value": 3}
    assert registry.normalize(normalized) is normalized


def test_registry_orders_schemas_and_freezes_only_complete_chains() -> None:
    registry = MessageSchemaRegistry(max_schemas=2)
    registry.register_schema(
        MessageSchema("z.changed", EnvelopeKind.DOMAIN_EVENT, current_version=1)
    )
    registry.register_schema(
        MessageSchema("a.changed", EnvelopeKind.INTEGRATION_EVENT, current_version=2)
    )

    assert [schema.message_type for schema in registry.schemas] == ["a.changed", "z.changed"]
    with pytest.raises(InvalidMessageSchema, match="incomplete"):
        registry.freeze()

    registry.register_upcaster("a.changed", 1, lambda payload: payload)
    registry.freeze()
    assert registry.frozen
    registry.freeze()

    with pytest.raises(InvalidMessageSchema, match="frozen"):
        registry.register_schema(
            MessageSchema("later.changed", EnvelopeKind.DOMAIN_EVENT, current_version=1)
        )
    with pytest.raises(InvalidMessageSchema, match="frozen"):
        registry.register_upcaster("a.changed", 1, lambda payload: payload)


def test_registry_rejects_duplicate_unknown_invalid_and_unsupported_contracts() -> None:
    registry = _registry(current_version=2)

    with pytest.raises(InvalidMessageSchema, match="already registered"):
        registry.register_schema(
            MessageSchema("tasks.changed", EnvelopeKind.DOMAIN_EVENT, current_version=2)
        )
    with pytest.raises(UnsupportedMessageSchema, match="not registered"):
        registry.register_upcaster("unknown.changed", 1, lambda payload: payload)
    with pytest.raises(InvalidMessageSchema, match="outside"):
        registry.register_upcaster("tasks.changed", 2, lambda payload: payload)

    registry.register_upcaster("tasks.changed", 1, lambda payload: payload)
    with pytest.raises(InvalidMessageSchema, match="already registered"):
        registry.register_upcaster("tasks.changed", 1, lambda payload: payload)
    registry.freeze()

    with pytest.raises(UnsupportedMessageSchema, match="kind"):
        registry.normalize(_envelope(kind=EnvelopeKind.INTEGRATION_EVENT))
    with pytest.raises(UnsupportedMessageSchema, match="version"):
        registry.normalize(_envelope(version=3))
    with pytest.raises(UnsupportedMessageSchema, match="not registered"):
        registry.normalize(_envelope(message_type="unknown.changed"))


@pytest.mark.parametrize(
    "schema",
    [
        MessageSchema("tasks.changed", EnvelopeKind.DOMAIN_EVENT, 1),
        MessageSchema("tasks.changed", EnvelopeKind.DOMAIN_EVENT, 2, 2),
    ],
)
def test_schema_accepts_finite_valid_intervals(schema: MessageSchema) -> None:
    assert schema.minimum_version <= schema.current_version


def test_schema_and_registry_configuration_fail_closed() -> None:
    with pytest.raises(InvalidMessageSchema):
        MessageSchema("Not Canonical", EnvelopeKind.DOMAIN_EVENT, 1)
    with pytest.raises(InvalidMessageSchema):
        MessageSchema("tasks.changed", EnvelopeKind.DOMAIN_EVENT, 0)
    with pytest.raises(InvalidMessageSchema):
        MessageSchema("tasks.changed", EnvelopeKind.DOMAIN_EVENT, 1, 2)
    with pytest.raises(InvalidMessageSchema, match="maximum upcast"):
        MessageSchema("tasks.changed", EnvelopeKind.DOMAIN_EVENT, 258, 1)
    with pytest.raises(InvalidMessageSchema):
        MessageSchemaRegistry(max_schemas=0)
    with pytest.raises(InvalidMessageSchema):
        MessageSchemaRegistry(payload_limits=object())  # type: ignore[arg-type]


def test_registry_rejects_async_upcasters_and_sanitizes_upcast_failures() -> None:
    registry = _registry(current_version=2)

    async def asynchronous(payload: object) -> object:
        return payload

    with pytest.raises(InvalidMessageSchema, match="synchronous"):
        registry.register_upcaster("tasks.changed", 1, asynchronous)

    def broken(payload: object) -> object:
        del payload
        raise RuntimeError("private-payload-fragment")

    registry.register_upcaster("tasks.changed", 1, broken)
    registry.freeze()

    with pytest.raises(MessageUpcastError) as caught:
        registry.normalize(_envelope())
    assert "private-payload-fragment" not in str(caught.value)


def test_registry_rejects_async_and_invalid_upcaster_results() -> None:
    async def result() -> object:
        return {"ok": True}

    registry = _registry(current_version=2)
    registry.register_upcaster("tasks.changed", 1, lambda payload: result())
    registry.freeze()
    with pytest.raises(MessageUpcastError, match="asynchronous"):
        registry.normalize(_envelope())

    bounded = MessageSchemaRegistry(payload_limits=PayloadLimits(max_string_bytes=3))
    bounded.register_schema(
        MessageSchema("tasks.changed", EnvelopeKind.DOMAIN_EVENT, current_version=2)
    )
    bounded.register_upcaster("tasks.changed", 1, lambda payload: {"value": "long"})
    bounded.freeze()
    with pytest.raises(MessageUpcastError, match="invalid data"):
        bounded.normalize(_envelope())


def test_local_bus_publishes_current_schema_to_sync_listeners_in_order() -> None:
    registry = _registry(current_version=2)
    registry.register_upcaster(
        "tasks.changed",
        1,
        lambda payload: {"value": 2, "source": payload},
    )
    bus = LocalEventBus(registry)
    observed: list[tuple[str, int, object]] = []

    def first(envelope: MessageEnvelope) -> None:
        observed.append(("first", envelope.metadata.schema_version, envelope.payload_copy()))

    def second(envelope: MessageEnvelope) -> None:
        observed.append(("second", envelope.metadata.schema_version, envelope.payload_copy()))

    bus.subscribe("tasks.changed", first)
    bus.subscribe("tasks.changed", second)
    bus.freeze()

    report = bus.publish(_envelope())

    assert observed == [
        ("first", 2, {"source": {"value": 1}, "value": 2}),
        ("second", 2, {"source": {"value": 1}, "value": 2}),
    ]
    assert report.message_type == "tasks.changed"
    assert report.schema_version == 2
    assert report.listener_count == 2


def test_local_bus_async_publication_preserves_mixed_listener_order() -> None:
    registry = _registry()
    bus = LocalEventBus(registry)
    observed: list[str] = []

    def synchronous(envelope: MessageEnvelope) -> None:
        assert envelope.metadata.schema_version == 1
        observed.append("sync")

    async def asynchronous(envelope: MessageEnvelope) -> None:
        assert envelope.metadata.message_type == "tasks.changed"
        await asyncio.sleep(0)
        observed.append("async")

    bus.subscribe("tasks.changed", synchronous)
    bus.subscribe("tasks.changed", asynchronous)
    bus.freeze()

    report = asyncio.run(bus.publish_async(_envelope()))

    assert observed == ["sync", "async"]
    assert report.listener_count == 2


def test_local_bus_no_listener_publication_is_an_explicit_zero_count() -> None:
    registry = _registry()
    bus = LocalEventBus(registry)
    bus.freeze()

    assert bus.publish(_envelope()).listener_count == 0
    assert asyncio.run(bus.publish_async(_envelope())).listener_count == 0


def test_sync_publish_preflights_async_listeners_before_any_side_effect() -> None:
    registry = _registry()
    bus = LocalEventBus(registry)
    observed: list[str] = []

    def synchronous(envelope: MessageEnvelope) -> None:
        del envelope
        observed.append("sync")

    async def asynchronous(envelope: MessageEnvelope) -> None:
        del envelope

    bus.subscribe("tasks.changed", synchronous)
    bus.subscribe("tasks.changed", asynchronous)
    bus.freeze()

    with pytest.raises(InvalidMessageSchema, match="asynchronous"):
        bus.publish(_envelope())
    assert observed == []


def test_listener_failure_and_cancellation_stop_later_listeners() -> None:
    registry = _registry()
    bus = LocalEventBus(registry)
    observed: list[str] = []

    def failing(envelope: MessageEnvelope) -> None:
        del envelope
        observed.append("failing")
        raise RuntimeError("private-event-content")

    def later(envelope: MessageEnvelope) -> None:
        del envelope
        observed.append("later")

    bus.subscribe("tasks.changed", failing)
    bus.subscribe("tasks.changed", later)
    bus.freeze()

    with pytest.raises(LocalEventListenerError) as caught:
        bus.publish(_envelope())
    assert "private-event-content" not in str(caught.value)
    assert observed == ["failing"]

    cancel_registry = _registry()
    cancel_bus = LocalEventBus(cancel_registry)
    cancel_observed: list[str] = []

    async def cancelled(envelope: MessageEnvelope) -> None:
        del envelope
        cancel_observed.append("cancelled")
        raise asyncio.CancelledError

    async def never(envelope: MessageEnvelope) -> None:
        del envelope
        cancel_observed.append("never")

    cancel_bus.subscribe("tasks.changed", cancelled)
    cancel_bus.subscribe("tasks.changed", never)
    cancel_bus.freeze()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_bus.publish_async(_envelope()))
    assert cancel_observed == ["cancelled"]


def test_local_bus_requires_event_schemas_and_frozen_declarations() -> None:
    command_registry = _registry(kind=EnvelopeKind.COMMAND)
    command_bus = LocalEventBus(command_registry)
    with pytest.raises(InvalidMessageSchema, match="only domain or integration"):
        command_bus.subscribe("tasks.changed", lambda envelope: None)

    registry = _registry()
    bus = LocalEventBus(registry, max_listeners_per_event=1)
    listener = lambda envelope: None
    bus.subscribe("tasks.changed", listener)
    with pytest.raises(InvalidMessageSchema, match="already registered"):
        bus.subscribe("tasks.changed", listener)
    with pytest.raises(InvalidMessageSchema, match="limit"):
        bus.subscribe("tasks.changed", lambda envelope: None)
    with pytest.raises(InvalidMessageSchema, match="frozen before publication"):
        bus.publish(_envelope())

    bus.freeze()
    with pytest.raises(InvalidMessageSchema, match="frozen"):
        bus.subscribe("tasks.changed", lambda envelope: None)
    with pytest.raises(UnsupportedMessageSchema, match="not registered"):
        bus.publish(_envelope(message_type="unknown.changed"))


def test_local_bus_rejects_listener_results_that_claim_new_semantics() -> None:
    registry = _registry()
    bus = LocalEventBus(registry)
    bus.subscribe("tasks.changed", lambda envelope: envelope)
    bus.freeze()

    with pytest.raises(InvalidMessageSchema, match="return None"):
        bus.publish(_envelope())

    async_registry = _registry()
    async_bus = LocalEventBus(async_registry)

    async def listener(envelope: MessageEnvelope) -> str:
        del envelope
        return "not-allowed"

    async_bus.subscribe("tasks.changed", listener)
    async_bus.freeze()
    with pytest.raises(InvalidMessageSchema, match="return None"):
        asyncio.run(async_bus.publish_async(_envelope()))
