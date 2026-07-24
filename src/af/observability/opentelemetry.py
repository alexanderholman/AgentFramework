"""Dependency-free OpenTelemetry bridge for bounded AF telemetry signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isasyncgen, isawaitable, iscoroutinefunction
from typing import Protocol

from af.observability.signals import HealthState, TelemetryKind, TelemetrySignal

__all__ = ("OpenTelemetryExporter", "OpenTelemetryRecord", "OpenTelemetrySink")

type OpenTelemetryAttributeValue = bool | int | float | str

_SCHEMA_VERSION = "af.opentelemetry.v1"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _unix_nano(value: datetime) -> int:
    normalized = value.astimezone(UTC)
    delta = normalized - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _health_attributes(
    state: HealthState | None,
) -> tuple[tuple[str, OpenTelemetryAttributeValue], ...]:
    if state is None:
        return ()
    return (
        ("af.health.capability", state.capability.value),
        ("af.health.degradation", state.degradation.value),
        ("af.health.fresh_until_unix_nano", _unix_nano(state.fresh_until)),
        ("af.health.liveness", state.liveness.value),
        ("af.health.readiness", state.readiness.value),
        ("af.health.reason_code", state.reason_code),
    )


@dataclass(frozen=True, slots=True, repr=False)
class OpenTelemetryRecord:
    """Primitive SDK-neutral record preserving AF telemetry semantics."""

    schema_version: str
    instrumentation_scope: str
    kind: TelemetryKind
    name: str
    time_unix_nano: int
    attributes: tuple[tuple[str, OpenTelemetryAttributeValue], ...]
    operation: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    trace_flags: int = 0
    tenant_ref: str | None = None

    @classmethod
    def from_signal(
        cls,
        signal: TelemetrySignal,
        /,
        *,
        instrumentation_scope: str = "af",
    ) -> OpenTelemetryRecord:
        if type(signal) is not TelemetrySignal:
            raise TypeError("OpenTelemetry projection requires a TelemetrySignal")
        if type(instrumentation_scope) is not str or not instrumentation_scope:
            raise ValueError("OpenTelemetry instrumentation scope must be non-empty text")
        context = signal.context
        attributes = tuple(
            sorted(
                (
                    *((f"af.attribute.{key}", value) for key, value in signal.attributes),
                    *_health_attributes(signal.health_state),
                ),
                key=lambda item: item[0],
            )
        )
        return cls(
            schema_version=_SCHEMA_VERSION,
            instrumentation_scope=instrumentation_scope,
            kind=signal.kind,
            name=signal.name,
            time_unix_nano=_unix_nano(signal.observed_at),
            attributes=attributes,
            operation=None if context is None else context.operation,
            correlation_id=None if context is None else context.correlation_id,
            causation_id=None if context is None else context.causation_id,
            trace_id=None if context is None else context.trace_id,
            span_id=None if context is None else context.span_id,
            trace_flags=0 if context is None else context.trace_flags,
            tenant_ref=None if context is None else context.tenant_ref,
        )

    def __repr__(self) -> str:
        return (
            f"OpenTelemetryRecord(kind={self.kind.value!r}, name={self.name!r}, "
            f"time_unix_nano={self.time_unix_nano!r}, "
            "context='[REDACTED]', attributes='[REDACTED]')"
        )


class OpenTelemetryExporter(Protocol):
    """Optional synchronous integration boundary implemented outside trusted core."""

    def export(self, record: OpenTelemetryRecord, /) -> object: ...


class OpenTelemetrySink:
    """Project AF signals into records consumable by optional OpenTelemetry integrations."""

    def __init__(
        self,
        exporter: OpenTelemetryExporter,
        *,
        instrumentation_scope: str = "af",
    ) -> None:
        try:
            export = exporter.export
        except Exception as error:
            raise ValueError("OpenTelemetry exporter is invalid") from error
        if not callable(export) or iscoroutinefunction(export):
            raise ValueError("OpenTelemetry exporter must expose synchronous export")
        if type(instrumentation_scope) is not str or not instrumentation_scope:
            raise ValueError("OpenTelemetry instrumentation scope must be non-empty text")
        self._exporter = exporter
        self._instrumentation_scope = instrumentation_scope

    def emit(self, signal: TelemetrySignal, /) -> None:
        record = OpenTelemetryRecord.from_signal(
            signal,
            instrumentation_scope=self._instrumentation_scope,
        )
        result = self._exporter.export(record)
        if isawaitable(result) or isasyncgen(result):
            close = getattr(result, "close", None)
            if callable(close):
                close()
            else:
                cancel = getattr(result, "cancel", None)
                if callable(cancel):
                    cancel()
            raise TypeError("OpenTelemetry exporter returned an asynchronous result")
