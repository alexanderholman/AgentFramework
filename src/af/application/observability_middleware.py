"""Optional best-effort observers after shared task dispatch and persistence."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Callable
from contextlib import suppress
from uuid import uuid4

from af.application.bus import ApplicationResult, CallNext, DispatchRequest
from af.application.dispatch import CountCharacters, CreateTask, ExecuteTask, UpdateTask
from af.application.observability_products import InspectorService, MetricsService
from af.framework.observability_products import InspectorKind


class ObservationMiddleware:
    def __init__(
        self,
        *,
        inspector: InspectorService | None = None,
        metrics: MetricsService | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._inspector, self._metrics, self._clock = inspector, metrics, clock

    async def __call__(
        self, request: DispatchRequest, call_next: CallNext
    ) -> ApplicationResult[object]:
        operation = {
            ExecuteTask: "execute",
            CreateTask: "create",
            UpdateTask: "update",
            CountCharacters: "count",
        }.get(type(request.message))
        if operation is None:
            return await call_next(request)
        started: float | None = None
        with suppress(Exception):
            started = self._clock()
        try:
            result = await call_next(request)
        except asyncio.CancelledError:
            self._record(request, operation, "cancelled", started)
            raise
        except Exception:
            self._record(request, operation, "error", started)
            raise
        self._record(request, operation, "ok", started)
        return result

    def _record(
        self, request: DispatchRequest, operation: str, outcome: str, started: float | None
    ) -> None:
        # A telemetry clock or optional service failure must not change the result.
        with suppress(Exception):
            elapsed = None if started is None else max(0.0, (self._clock() - started) * 1000)
            tenant = request.context.tenant_id
            tenant_ref = (
                None if tenant is None else "sha256:" + hashlib.sha256(tenant.encode()).hexdigest()
            )
            inspector = self._inspector
            if inspector is not None and tenant_ref in (None, inspector.tenant_ref):
                with suppress(Exception):
                    fields: dict[str, object] = {"status": outcome}
                    if elapsed is not None:
                        fields["duration_ms"] = elapsed
                    inspector.collect(
                        InspectorKind.JOB,
                        "af.task.completed",
                        event_id=uuid4().hex,
                        fields=fields,
                        correlation_id=request.context.correlation_id,
                    )
            metrics = self._metrics
            if metrics is not None and tenant_ref in (None, metrics.tenant_ref):
                labels = {"operation": operation, "outcome": outcome}
                with suppress(Exception):
                    metrics.record("af.task.completed", 1, labels)
                    if elapsed is not None:
                        metrics.record("af.task.duration", elapsed, labels)
