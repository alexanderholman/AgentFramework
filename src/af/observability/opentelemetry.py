"""Dependency-free OpenTelemetry bridge for bounded AF telemetry signals."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isasyncgen, isawaitable, iscoroutinefunction
from math import isfinite
from typing import Protocol

from af.observability.signals import HealthState, TelemetryKind, TelemetrySignal

__all__ = ("OpenTelemetryExporter", "OpenTelemetryRecord", "OpenTelemetrySink")

type OpenTelemetryAttributeValue = bool | int | float | str

_SCHEMA_VERSION = "af.opentelemetry.v1"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_ATTRIBUTE_KEY = re.compile(r"^[a-z][a-z0-9_.-]{0,126}[a-z0-9]$|^[a-z]$")
_ATTRIBUTE_TOKEN = re.compile(r"^[a-z0-9](?:[a-z0-9_.:-]{0,198}[a-z0-9])?$")
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_.-])(?:authorization|cookie|credential|document|password|payload|"
    r"prompt|response|secret|token|vector|content)(?:$|[_.-])"
)
_TENANT_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_MAX_ATTRIBUTES = 40
_MAX_VALUE_LENGTH = 200


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


def _opaque(value: object, label: str) -> None:
    if value is None:
        return
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_VALUE_LENGTH
        or _UNSAFE_TEXT.search(value)
    ):
        raise ValueError(f"{label} must be bounded non-empty single-line text")


def _attribute_value(value: object) -> OpenTelemetryAttributeValue:
    if type(value) is bool:
        return value
    if type(value) is int:
        if not -(2**63) <= value < 2**63:
            raise ValueError("OpenTelemetry integer attributes must be signed 64-bit values")
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("OpenTelemetry float attributes must be finite")
        return value
    if type(value) is str and _ATTRIBUTE_TOKEN.fullmatch(value) is not None:
        return value
    raise ValueError("OpenTelemetry string attributes must be bounded low-cardinality tokens")


def _validate_attributes(
    attributes: object,
) -> tuple[tuple[str, OpenTelemetryAttributeValue], ...]:
    if type(attributes) is not tuple or len(attributes) > _MAX_ATTRIBUTES:
        raise ValueError(f"OpenTelemetry records allow at most {_MAX_ATTRIBUTES} attributes")
    validated: list[tuple[str, OpenTelemetryAttributeValue]] = []
    for item in attributes:
        if type(item) is not tuple or len(item) != 2:
            raise ValueError("OpenTelemetry attributes must be key-value pairs")
        key, value = item
        if type(key) is not str or _ATTRIBUTE_KEY.fullmatch(key) is None:
            raise ValueError("OpenTelemetry attribute keys must be canonical identifiers")
        if _SENSITIVE_KEY.search(key):
            raise ValueError("OpenTelemetry attributes cannot use sensitive keys")
        validated.append((key, _attribute_value(value)))
    canonical = tuple(sorted(validated, key=lambda item: item[0]))
    if len(dict(canonical)) != len(canonical) or canonical != attributes:
        raise ValueError("OpenTelemetry attributes must be unique and canonically sorted")
    return canonical


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

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("OpenTelemetry record schema version is unsupported")
        if (
            type(self.instrumentation_scope) is not str
            or _IDENTIFIER.fullmatch(self.instrumentation_scope) is None
        ):
            raise ValueError("OpenTelemetry instrumentation scope must be canonical")
        if not isinstance(self.kind, TelemetryKind):
            raise TypeError("OpenTelemetry record kind is invalid")
        if type(self.name) is not str or _IDENTIFIER.fullmatch(self.name) is None:
            raise ValueError("OpenTelemetry record name must be canonical")
        if type(self.time_unix_nano) is not int or self.time_unix_nano < 0:
            raise ValueError("OpenTelemetry event time must be non-negative Unix nanoseconds")
        _validate_attributes(self.attributes)
        if self.operation is not None and (
            type(self.operation) is not str or _IDENTIFIER.fullmatch(self.operation) is None
        ):
            raise ValueError("OpenTelemetry operation must be canonical")
        for value, label in (
            (self.correlation_id, "OpenTelemetry correlation id"),
            (self.causation_id, "OpenTelemetry causation id"),
            (self.trace_id, "OpenTelemetry trace id"),
            (self.span_id, "OpenTelemetry span id"),
        ):
            _opaque(value, label)
        if type(self.trace_flags) is not int or not 0 <= self.trace_flags <= 255:
            raise ValueError("OpenTelemetry trace flags must be an unsigned byte")
        if self.trace_id is None and (self.span_id is not None or self.trace_flags):
            raise ValueError("OpenTelemetry span and flags require a trace id")
        if self.tenant_ref is not None and (
            type(self.tenant_ref) is not str
            or _TENANT_REF.fullmatch(self.tenant_ref) is None
        ):
            raise ValueError("OpenTelemetry tenant reference must be an opaque sha256 digest")

    @classmethod
    def from_signal(
        cls,
        signal: TelemetrySignal,
        /,
        *,
        instrumentation_scope: str = "af",
    ) -> OpenTelemetryRecord:
        """Project one already-validated signal into the optional adapter boundary."""

        if type(signal) is not TelemetrySignal:
            raise TypeError("OpenTelemetry projection requires a TelemetrySignal")
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
        if type(instrumentation_scope) is not str or _IDENTIFIER.fullmatch(instrumentation_scope) is None:
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
