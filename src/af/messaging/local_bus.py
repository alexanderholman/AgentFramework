"""Deterministic in-process event dispatch over registered message schemas."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from af.messaging.envelopes import EnvelopeKind, MessageEnvelope
from af.messaging.errors import (
    InvalidMessageSchema,
    LocalEventListenerError,
    UnsupportedMessageSchema,
)
from af.messaging.schema import MessageSchemaRegistry

__all__ = (
    "LocalEventBus",
    "LocalEventDispatchReport",
    "LocalEventListener",
)

_EVENT_KINDS = frozenset({EnvelopeKind.DOMAIN_EVENT, EnvelopeKind.INTEGRATION_EVENT})
_MAX_LISTENERS_PER_EVENT = 4_096

type LocalEventListener = Callable[[MessageEnvelope], object]


def _is_async_callable(value: object) -> bool:
    if inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value):
        return True
    call = getattr(value, "__call__", None)
    return inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)


@dataclass(frozen=True, slots=True)
class LocalEventDispatchReport:
    """Count-only local dispatch evidence that contains no event payload."""

    message_type: str
    schema_version: int
    listener_count: int


class LocalEventBus:
    """Ordered sync/async event listeners frozen before publication begins."""

    def __init__(
        self,
        registry: MessageSchemaRegistry,
        *,
        max_listeners_per_event: int = 256,
    ) -> None:
        if not isinstance(registry, MessageSchemaRegistry):
            raise InvalidMessageSchema("local event bus schema registry is invalid")
        if (
            type(max_listeners_per_event) is not int
            or not 1 <= max_listeners_per_event <= _MAX_LISTENERS_PER_EVENT
        ):
            raise InvalidMessageSchema("local event listener limit is invalid")
        self._registry = registry
        self._max_listeners_per_event = max_listeners_per_event
        self._listeners: dict[tuple[str, EnvelopeKind], list[LocalEventListener]] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def registry(self) -> MessageSchemaRegistry:
        return self._registry

    def subscribe(
        self,
        message_type: str,
        listener: LocalEventListener,
    ) -> None:
        if self._frozen:
            raise InvalidMessageSchema("local event bus is frozen")
        if not callable(listener) or inspect.isasyncgenfunction(listener):
            raise InvalidMessageSchema("local event listener must be callable")
        schema = self._registry.schema_for(message_type)
        if schema.kind not in _EVENT_KINDS:
            raise InvalidMessageSchema("local event bus accepts only domain or integration events")
        key = (schema.message_type, schema.kind)
        listeners = self._listeners.setdefault(key, [])
        if any(registered is listener for registered in listeners):
            raise InvalidMessageSchema("local event listener is already registered")
        if len(listeners) >= self._max_listeners_per_event:
            raise InvalidMessageSchema("local event listener limit is exceeded")
        listeners.append(listener)

    def freeze(self) -> None:
        if self._frozen:
            return
        self._registry.freeze()
        self._frozen = True

    def publish(self, envelope: MessageEnvelope) -> LocalEventDispatchReport:
        """Publish to synchronous listeners without creating an event loop."""

        normalized, listeners = self._prepare(envelope)
        if any(_is_async_callable(listener) for listener in listeners):
            raise InvalidMessageSchema(
                "synchronous local publication cannot invoke asynchronous listeners"
            )
        for listener in listeners:
            try:
                result = listener(normalized)
            except Exception as error:
                raise LocalEventListenerError("local event listener failed") from error
            self._validate_sync_result(result)
        return LocalEventDispatchReport(
            message_type=normalized.metadata.message_type,
            schema_version=normalized.metadata.schema_version,
            listener_count=len(listeners),
        )

    async def publish_async(self, envelope: MessageEnvelope) -> LocalEventDispatchReport:
        """Publish sequentially to synchronous or asynchronous listeners."""

        normalized, listeners = self._prepare(envelope)
        for listener in listeners:
            try:
                result = listener(normalized)
                if inspect.isasyncgen(result):
                    raise InvalidMessageSchema(
                        "local event listeners cannot return asynchronous generators"
                    )
                if inspect.isawaitable(result):
                    result = await cast(Awaitable[object], result)
            except asyncio.CancelledError:
                raise
            except InvalidMessageSchema:
                raise
            except Exception as error:
                raise LocalEventListenerError("local event listener failed") from error
            if result is not None:
                raise InvalidMessageSchema("local event listeners must return None")
        return LocalEventDispatchReport(
            message_type=normalized.metadata.message_type,
            schema_version=normalized.metadata.schema_version,
            listener_count=len(listeners),
        )

    def _prepare(
        self,
        envelope: MessageEnvelope,
    ) -> tuple[MessageEnvelope, tuple[LocalEventListener, ...]]:
        if not self._frozen:
            raise InvalidMessageSchema("local event bus must be frozen before publication")
        normalized = self._registry.normalize(envelope)
        if normalized.metadata.kind not in _EVENT_KINDS:
            raise UnsupportedMessageSchema(
                "local event bus accepts only domain or integration events"
            )
        key = (normalized.metadata.message_type, normalized.metadata.kind)
        return normalized, tuple(self._listeners.get(key, ()))

    @staticmethod
    def _validate_sync_result(result: object) -> None:
        if inspect.iscoroutine(result):
            result.close()
            raise InvalidMessageSchema(
                "synchronous local publication received asynchronous listener work"
            )
        if inspect.isawaitable(result) or inspect.isasyncgen(result):
            raise InvalidMessageSchema(
                "synchronous local publication received asynchronous listener work"
            )
        if result is not None:
            raise InvalidMessageSchema("local event listeners must return None")
