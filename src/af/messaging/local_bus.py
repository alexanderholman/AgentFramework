"""Deterministic in-process event dispatch over registered message schemas."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from af.messaging.envelopes import EnvelopeKind, MessageEnvelope
from af.messaging.errors import (
    InvalidMessageSchema,
    LocalEventListenerError,
    LocalEventMiddlewareError,
    UnsupportedMessageSchema,
)
from af.messaging.schema import MessageSchemaRegistry
from af.observability.signals import TelemetryKind, TelemetryPublisher, TelemetrySignal

__all__ = (
    "AsyncLocalEventCallNext",
    "LocalEventBus",
    "LocalEventCallNext",
    "LocalEventDispatchReport",
    "LocalEventListener",
    "LocalEventMiddleware",
    "LocalEventTelemetryMiddleware",
)

_EVENT_KINDS = frozenset({EnvelopeKind.DOMAIN_EVENT, EnvelopeKind.INTEGRATION_EVENT})
_MAX_LISTENERS_PER_EVENT = 4_096
_MAX_MIDDLEWARE = 256

type LocalEventListener = Callable[[MessageEnvelope], object]


@dataclass(frozen=True, slots=True)
class LocalEventDispatchReport:
    """Count-only local dispatch evidence that contains no event payload."""

    message_type: str
    schema_version: int
    listener_count: int


type LocalEventCallNext = Callable[[], LocalEventDispatchReport]
type AsyncLocalEventCallNext = Callable[[], Awaitable[LocalEventDispatchReport]]


class LocalEventMiddleware(Protocol):
    """One deterministic around-dispatch extension for both local bus modes."""

    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport: ...

    def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> Awaitable[LocalEventDispatchReport]: ...


class LocalEventTelemetryMiddleware:
    """Project bounded local-dispatch telemetry without exposing message payloads."""

    def __init__(
        self,
        publisher: TelemetryPublisher,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(publisher, TelemetryPublisher):
            raise InvalidMessageSchema("local event telemetry publisher is invalid")
        if clock is not None and not callable(clock):
            raise InvalidMessageSchema("local event telemetry clock is invalid")
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))

    def dispatch(
        self,
        envelope: MessageEnvelope,
        call_next: LocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        self._emit("started", envelope, mode="sync")
        try:
            report = call_next()
        except asyncio.CancelledError:
            self._emit("cancelled", envelope, mode="sync")
            raise
        except Exception:
            self._emit("failed", envelope, mode="sync")
            raise
        self._emit("completed", envelope, mode="sync", report=report)
        return report

    async def dispatch_async(
        self,
        envelope: MessageEnvelope,
        call_next: AsyncLocalEventCallNext,
        /,
    ) -> LocalEventDispatchReport:
        self._emit("started", envelope, mode="async")
        try:
            report = await call_next()
        except asyncio.CancelledError:
            self._emit("cancelled", envelope, mode="async")
            raise
        except Exception:
            self._emit("failed", envelope, mode="async")
            raise
        self._emit("completed", envelope, mode="async", report=report)
        return report

    def _emit(
        self,
        stage: str,
        envelope: MessageEnvelope,
        *,
        mode: str,
        report: LocalEventDispatchReport | None = None,
    ) -> None:
        attributes: dict[str, object] = {
            "dispatch.mode": mode,
            "message.kind": envelope.metadata.kind.value,
            "message.schema_version": envelope.metadata.schema_version,
            "message.type": envelope.metadata.message_type,
            "stage": stage,
        }
        if report is not None:
            attributes["listener.count"] = report.listener_count
        try:
            signal = TelemetrySignal.create(
                kind=TelemetryKind.SPAN,
                name=f"messaging.local_event.{stage}",
                observed_at=self._clock(),
                context=envelope.metadata.telemetry,
                attributes=attributes,
            )
            self._publisher.publish(signal)
        except Exception:
            # Telemetry is optional evidence and cannot change dispatch semantics.
            return


class LocalEventBus:
    """Ordered sync/async event listeners frozen before publication begins."""

    def __init__(
        self,
        registry: MessageSchemaRegistry,
        *,
        max_listeners_per_event: int = 256,
        middleware: Iterable[LocalEventMiddleware] = (),
    ) -> None:
        if not isinstance(registry, MessageSchemaRegistry):
            raise InvalidMessageSchema("local event bus schema registry is invalid")
        if (
            type(max_listeners_per_event) is not int
            or not 1 <= max_listeners_per_event <= _MAX_LISTENERS_PER_EVENT
        ):
            raise InvalidMessageSchema("local event listener limit is invalid")
        middleware_snapshot = tuple(middleware)
        if len(middleware_snapshot) > _MAX_MIDDLEWARE:
            raise InvalidMessageSchema("local event middleware limit is exceeded")
        for extension in middleware_snapshot:
            if not callable(getattr(extension, "dispatch", None)) or not callable(
                getattr(extension, "dispatch_async", None)
            ):
                raise InvalidMessageSchema(
                    "local event middleware must define sync and async dispatch"
                )
            if inspect.iscoroutinefunction(
                extension.dispatch
            ) or inspect.isasyncgenfunction(extension.dispatch):
                raise InvalidMessageSchema(
                    "local event synchronous middleware dispatch cannot be asynchronous"
                )
            if inspect.isasyncgenfunction(extension.dispatch_async):
                raise InvalidMessageSchema(
                    "local event asynchronous middleware cannot be an asynchronous generator"
                )
        self._registry = registry
        self._max_listeners_per_event = max_listeners_per_event
        self._middleware = middleware_snapshot
        self._listeners: dict[tuple[str, EnvelopeKind], list[LocalEventListener]] = {}
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    @property
    def registry(self) -> MessageSchemaRegistry:
        return self._registry

    @property
    def middleware(self) -> tuple[LocalEventMiddleware, ...]:
        return self._middleware

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
        return self._invoke_sync_middleware(normalized, listeners, 0)

    async def publish_async(self, envelope: MessageEnvelope) -> LocalEventDispatchReport:
        """Publish sequentially to synchronous or asynchronous listeners."""

        normalized, listeners = self._prepare(envelope)
        return await self._invoke_async_middleware(normalized, listeners, 0)

    def _invoke_sync_middleware(
        self,
        envelope: MessageEnvelope,
        listeners: tuple[LocalEventListener, ...],
        index: int,
    ) -> LocalEventDispatchReport:
        if index == len(self._middleware):
            return self._dispatch_sync(envelope, listeners)

        extension = self._middleware[index]
        called = False

        def call_next() -> LocalEventDispatchReport:
            nonlocal called
            if called:
                raise InvalidMessageSchema("local event middleware called next more than once")
            called = True
            return self._invoke_sync_middleware(envelope, listeners, index + 1)

        try:
            report = extension.dispatch(envelope, call_next)
        except asyncio.CancelledError:
            raise
        except (
            InvalidMessageSchema,
            UnsupportedMessageSchema,
            LocalEventListenerError,
            LocalEventMiddlewareError,
        ):
            raise
        except Exception as error:
            raise LocalEventMiddlewareError("local event middleware failed") from error

        if not called:
            raise InvalidMessageSchema("local event middleware must call next exactly once")
        return self._validate_report(report, envelope, len(listeners))

    async def _invoke_async_middleware(
        self,
        envelope: MessageEnvelope,
        listeners: tuple[LocalEventListener, ...],
        index: int,
    ) -> LocalEventDispatchReport:
        if index == len(self._middleware):
            return await self._dispatch_async(envelope, listeners)

        extension = self._middleware[index]
        called = False
        completed = False
        pending: Awaitable[LocalEventDispatchReport] | None = None

        async def continue_chain() -> LocalEventDispatchReport:
            nonlocal completed
            report = await self._invoke_async_middleware(envelope, listeners, index + 1)
            completed = True
            return report

        def call_next() -> Awaitable[LocalEventDispatchReport]:
            nonlocal called, pending
            if called:
                raise InvalidMessageSchema("local event middleware called next more than once")
            called = True
            pending = continue_chain()
            return pending

        try:
            result: object = extension.dispatch_async(envelope, call_next)
            if inspect.isasyncgen(result):
                await result.aclose()
                raise InvalidMessageSchema(
                    "local event asynchronous middleware returned an asynchronous generator"
                )
            if not inspect.isawaitable(result):
                self._close_pending(pending)
                raise InvalidMessageSchema(
                    "local event asynchronous middleware must return awaitable work"
                )
            report = await result
        except asyncio.CancelledError:
            raise
        except (
            InvalidMessageSchema,
            UnsupportedMessageSchema,
            LocalEventListenerError,
            LocalEventMiddlewareError,
        ):
            self._close_pending(pending)
            raise
        except Exception as error:
            self._close_pending(pending)
            raise LocalEventMiddlewareError("local event middleware failed") from error

        if not called or not completed:
            self._close_pending(pending)
            raise InvalidMessageSchema("local event middleware must await next exactly once")
        return self._validate_report(report, envelope, len(listeners))

    @staticmethod
    def _close_pending(pending: Awaitable[LocalEventDispatchReport] | None) -> None:
        if inspect.iscoroutine(pending):
            pending.close()

    @staticmethod
    def _validate_report(
        report: object,
        envelope: MessageEnvelope,
        listener_count: int,
    ) -> LocalEventDispatchReport:
        if type(report) is not LocalEventDispatchReport:
            raise InvalidMessageSchema("local event middleware returned invalid dispatch evidence")
        if (
            report.message_type != envelope.metadata.message_type
            or report.schema_version != envelope.metadata.schema_version
            or report.listener_count != listener_count
        ):
            raise InvalidMessageSchema("local event middleware altered dispatch evidence")
        return report

    @staticmethod
    def _dispatch_sync(
        envelope: MessageEnvelope,
        listeners: tuple[LocalEventListener, ...],
    ) -> LocalEventDispatchReport:
        for listener in listeners:
            try:
                result = listener(envelope)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise LocalEventListenerError("local event listener failed") from error
            LocalEventBus._validate_sync_result(result)
        return LocalEventDispatchReport(
            message_type=envelope.metadata.message_type,
            schema_version=envelope.metadata.schema_version,
            listener_count=len(listeners),
        )

    @staticmethod
    async def _dispatch_async(
        envelope: MessageEnvelope,
        listeners: tuple[LocalEventListener, ...],
    ) -> LocalEventDispatchReport:
        for listener in listeners:
            try:
                result = listener(envelope)
                if inspect.isawaitable(result):
                    result = await cast(Awaitable[object], result)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise LocalEventListenerError("local event listener failed") from error
            if inspect.isasyncgen(result):
                raise InvalidMessageSchema(
                    "local event listeners cannot return asynchronous generators"
                )
            if result is not None:
                raise InvalidMessageSchema("local event listeners must return None")
        return LocalEventDispatchReport(
            message_type=envelope.metadata.message_type,
            schema_version=envelope.metadata.schema_version,
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


def _is_async_callable(value: object) -> bool:
    if inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value):
        return True
    call = inspect.getattr_static(value, "__call__", None)
    return inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)
