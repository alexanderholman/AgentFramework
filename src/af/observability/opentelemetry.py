"""Dependency-free OpenTelemetry bridge for bounded AF telemetry signals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isasyncgen, isawaitable, iscoroutinefunction
from typing import Protocol

from af.observability.signals import HealthState, TelemetryKind, TelemetrySignal

__all__ = ("OpenTelemetryExporter", "OpenTelemetryRecord", "OpenTelemetrySink")

type OpenTelemetryAttributeValue = bool | int | float | str

_SCHEMA_VERSION = "af.opentelemetry.v1"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")


def _unix_nano(value: datetime) -> int:
    normalized = value.astimezone(UTC)
    delta = normalized - _EPOCH
    result = (delta.days * 86_400 + delta.seconds) * 1_000_000_000
    result += delta.microseconds * 1_000
    if result < 0:
        raise ValueError("OpenTelemetry event time cannot precede the Unix epoch")
    return result


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


@dataclass(frozen=True, slots=True, repr=False, init=False)
class OpenTelemetryRecord:
    """Factory-only SDK-neutral record preserving validated AF signal semantics."""

    schema_version: str
    instrumentation_scope: str
    kind: TelemetryKind
    name: str
    time_unix_nano: int
    attributes: tuple[tuple[str, OpenTelemetryAttributeValue], ...]
    operation: str | None
    correlation_id: str | None
    causation_id: str | None
    trace_id: str | None
    span_id: str | None
    trace_flags: int
    tenant_ref: str | None

    def __init__(self) -> None:
        raise TypeError("OpenTelemetry records must be created from a validated telemetry signal")

    @classmethod
    def from_signal(
        cls,
        signal: TelemetrySignal,
        /,
        *,
        instrumentation_scope: str = "af",
    ) -> OpenTelemetryRecord:
        """Project one validated signal without importing an OpenTelemetry SDK."""

        if type(signal) is not TelemetrySignal:
            raise TypeError("OpenTelemetry projection requires a TelemetrySignal")
        if (
            type(instrumentation_scope) is not str
            or _IDENTIFIER.fullmatch(instrumentation_scope) is None
        ):
            raise ValueError("OpenTelemetry instrumentation scope must be canonical")
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
        record = object.__new__(cls)
        object.__setattr__(record, "schema_version", _SCHEMA_VERSION)
        object.__setattr__(record, "instrumentation_scope", instrumentation_scope)
        object.__setattr__(record, "kind", signal.kind)
        object.__setattr__(record, "name", signal.name)
        object.__setattr__(record, "time_unix_nano", _unix_nano(signal.observed_at))
        object.__setattr__(record, "attributes", attributes)
        object.__setattr__(record, "operation", None if context is None else context.operation)
        object.__setattr__(
            record,
            "correlation_id",
            None if context is None else context.correlation_id,
        )
        object.__setattr__(
            record,
            "causation_id",
            None if context is None else context.causation_id,
        )
        object.__setattr__(record, "trace_id", None if context is None else context.trace_id)
        object.__setattr__(record, "span_id", None if context is None else context.span_id)
        object.__setattr__(record, "trace_flags", 0 if context is None else context.trace_flags)
        object.__setattr__(record, "tenant_ref", None if context is None else context.tenant_ref)
        return record

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
        if (
            type(instrumentation_scope) is not str
            or _IDENTIFIER.fullmatch(instrumentation_scope) is None
        ):
            raise ValueError("OpenTelemetry instrumentation scope must be canonical")
        self._exporter = exporter
        self._instrumentation_scope = instrumentation_scope

    def emit(self, signal: TelemetrySignal, /) -> None:
        """Project and export one signal; publisher isolation owns exporter failure."""

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
