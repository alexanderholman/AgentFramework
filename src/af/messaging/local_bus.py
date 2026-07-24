"""Deterministic in-process event dispatch over registered message schemas."""

from __future__ import annotations

import asyncio

from af.messaging._local_bus_core import (
    AsyncLocalEventCallNext,
    LocalEventBus as _LocalEventBus,
    LocalEventCallNext,
    LocalEventDispatchReport,
    LocalEventListener,
    LocalEventMiddleware,
    LocalEventTelemetryMiddleware as _LocalEventTelemetryMiddleware,
)
from af.messaging.envelopes import MessageEnvelope
from af.messaging.errors import (
    InvalidMessageSchema,
    LocalEventListenerError,
    LocalEventMiddlewareError,
    UnsupportedMessageSchema,
)

__all__ = (
    "AsyncLocalEventCallNext",
    "LocalEventBus",
    "LocalEventCallNext",
    "LocalEventDispatchReport",
    "LocalEventListener",
    "LocalEventMiddleware",
    "LocalEventTelemetryMiddleware",
)


class LocalEventTelemetryMiddleware(_LocalEventTelemetryMiddleware):
    """Isolate optional telemetry cancellation from dispatch semantics."""

    def _emit(
        self,
        stage: str,
        envelope: MessageEnvelope,
        *,
        mode: str,
        report: LocalEventDispatchReport | None = None,
    ) -> None:
        try:
            super()._emit(stage, envelope, mode=mode, report=report)
        except asyncio.CancelledError:
            # A synchronous optional telemetry hook cannot own dispatch cancellation.
            return


class LocalEventBus(_LocalEventBus):
    """Local event bus with fail-closed synchronous middleware completion."""

    def _invoke_sync_middleware(
        self,
        envelope: MessageEnvelope,
        listeners: tuple[LocalEventListener, ...],
        index: int,
    ) -> LocalEventDispatchReport:
        if index == len(self.middleware):
            return self._dispatch_sync(envelope, listeners)

        extension = self.middleware[index]
        called = False
        completed = False

        def call_next() -> LocalEventDispatchReport:
            nonlocal called, completed
            if called:
                raise InvalidMessageSchema("local event middleware called next more than once")
            called = True
            report = self._invoke_sync_middleware(envelope, listeners, index + 1)
            completed = True
            return report

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

        if not called or not completed:
            raise InvalidMessageSchema(
                "local event middleware must complete next exactly once"
            )
        return self._validate_report(report, envelope, len(listeners))
