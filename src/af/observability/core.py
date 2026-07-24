"""Bounded observability context, telemetry, and health contracts."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from threading import Lock
from typing import Protocol

from af.framework.context import ExecutionContext, TraceContext

__all__ = (
    "CapabilityStatus",
    "DegradationStatus",
    "HealthObservation",
    "LivenessStatus",
    "LogLevel",
    "LogRecord",
    "MetricPoint",
    "NoOpTelemetryExporter",
    "ReadinessStatus",
    "SafeTelemetry",
    "SpanRecord",
    "SpanStatus",
    "TelemetryContext",
    "TelemetryDiagnostics",
    "TelemetryExporter",
    "TelemetryPolicy",
    "UnsafeTelemetry",
    "UnsafeTelemetryReason",
)

type TelemetryScalar = str | int | float | bool
type TelemetryAttributes = tuple[tuple[str, TelemetryScalar], ...]

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_CONTEXT_PREFIX = "af.context."
_CONTEXT_KEYS = frozenset(
    {
        "af.context.version",
        "af.context.correlation_id",
        "af.context.causation_id",
        "af.context.trace_id",
        "af.context.span_id",
        "af.context.trace_flags",
        "af.context.operation_id",
        "af.context.tenant_scope",
    }
)
_SENSITIVE_FRAGMENTS = frozenset(
    {"authorization", "cookie", "credential", "password", "secret", "token"}
)
_FORBIDDEN_FRAGMENTS = frozenset(
    {"body", "content", "embedding", "path", "payload", "prompt", "vector"}
)
_HIGH_CARDINALITY_FRAGMENTS = frozenset(
    {
        "causation_id",
        "correlation_id",
        "request_id",
        "session_id",
        "span_id",
        "subject_id",
        "task_id",
        "tenant_id",
        "trace_id",
        "user_id",
    }
)


def _validate_identifier(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded lowercase identifier")


def _validate_optional_scalar(value: str | None, label: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > 200 or _CONTROL_CHARACTERS.search(value):
        raise ValueError(f"{label} must be a bounded non-empty single-line value")


def _validate_timestamp(value: datetime, label: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{label} must be a datetime")
    if value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")


@dataclass(frozen=True, slots=True, repr=False)
class TelemetryContext:
    """Transport-safe observability context without principal or raw tenant identity."""

    correlation_id: str | None = None
    causation_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    trace_flags: int = 0
    operation_id: str | None = None
    tenant_scope: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.correlation_id, "correlation id"),
            (self.causation_id, "causation id"),
            (self.trace_id, "trace id"),
            (self.span_id, "span id"),
            (self.operation_id, "operation id"),
            (self.tenant_scope, "tenant scope"),
        ):
            _validate_optional_scalar(value, label)
        if self.span_id is not None and self.trace_id is None:
            raise ValueError("span id requires a trace id")
        if type(self.trace_flags) is not int or not 0 <= self.trace_flags <= 255:
            raise ValueError("trace flags must be an integer from 0 to 255")

    def __repr__(self) -> str:
        return "TelemetryContext(identity='[REDACTED]', trace='[REDACTED]')"

    @classmethod
    def from_execution_context(
        cls,
        context: ExecutionContext,
        *,
        operation_id: str | None = None,
        tenant_scope: str | None = None,
    ) -> TelemetryContext:
        if not isinstance(context, ExecutionContext):
            raise TypeError("telemetry context requires an ExecutionContext")
        trace = context.trace
        return cls(
            correlation_id=context.correlation_id,
            causation_id=context.causation_id,
            trace_id=None if trace is None else trace.trace_id,
            span_id=None if trace is None else trace.span_id,
            trace_flags=0 if trace is None else trace.trace_flags,
            operation_id=operation_id,
            tenant_scope=tenant_scope,
        )

    def inject(self, carrier: MutableMapping[str, str]) -> None:
        unknown = tuple(
            key for key in carrier if key.startswith(_CONTEXT_PREFIX) and key not in _CONTEXT_KEYS
        )
        if unknown:
            raise ValueError("telemetry carrier contains unsupported AF context fields")
        for key in _CONTEXT_KEYS:
            carrier.pop(key, None)
        carrier["af.context.version"] = "1"
        values = (
            ("af.context.correlation_id", self.correlation_id),
            ("af.context.causation_id", self.causation_id),
            ("af.context.trace_id", self.trace_id),
            ("af.context.span_id", self.span_id),
            ("af.context.operation_id", self.operation_id),
            ("af.context.tenant_scope", self.tenant_scope),
        )
        for key, value in values:
            if value is not None:
                carrier[key] = value
        if self.trace_id is not None or self.trace_flags:
            carrier["af.context.trace_flags"] = str(self.trace_flags)

    @classmethod
    def extract(cls, carrier: Mapping[str, str]) -> TelemetryContext:
        unknown = tuple(
            key for key in carrier if key.startswith(_CONTEXT_PREFIX) and key not in _CONTEXT_KEYS
        )
        if unknown:
            raise ValueError("telemetry carrier contains unsupported AF context fields")
        version = carrier.get("af.context.version")
        if version != "1":
            raise ValueError("telemetry carrier version is unsupported")
        flags_text = carrier.get("af.context.trace_flags", "0")
        if not isinstance(flags_text, str) or not flags_text.isascii() or not flags_text.isdecimal():
            raise ValueError("telemetry carrier trace flags are invalid")
        values = {
            key: carrier.get(key)
            for key in _CONTEXT_KEYS
            if key != "af.context.version" and key != "af.context.trace_flags"
        }
        if any(value is not None and not isinstance(value, str) for value in values.values()):
            raise TypeError("telemetry carrier values must be strings")
        return cls(
            correlation_id=values["af.context.correlation_id"],
            causation_id=values["af.context.causation_id"],
            trace_id=values["af.context.trace_id"],
            span_id=values["af.context.span_id"],
            trace_flags=int(flags_text),
            operation_id=values["af.context.operation_id"],
            tenant_scope=values["af.context.tenant_scope"],
        )

    def to_execution_context(
        self,
        template: ExecutionContext | None = None,
    ) -> ExecutionContext:
        if template is not None and not isinstance(template, ExecutionContext):
            raise TypeError("telemetry context template must be an ExecutionContext")
        trace = (
            None
            if self.trace_id is None
            else TraceContext(self.trace_id, self.span_id, self.trace_flags)
        )
        if template is None:
            return ExecutionContext(
                correlation_id=self.correlation_id,
                causation_id=self.causation_id,
                trace=trace,
            )
        return ExecutionContext(
            principal=template.principal,
            tenant_id=template.tenant_id,
            correlation_id=self.correlation_id,
            causation_id=self.causation_id,
            deadline=template.deadline,
            trace=trace,
            locale=template.locale,
            requested_scope=template.requested_scope,
            effective_scope=template.effective_scope,
            cancellation=template.cancellation,
        )


class LivenessStatus(StrEnum):
    UNKNOWN = "unknown"
    LIVE = "live"
    DEAD = "dead"


class ReadinessStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_READY = "not-ready"
    WARMING = "warming"
    READY = "ready"
    BUSY = "busy"
    DRAINING = "draining"
    SATURATED = "saturated"


class DegradationStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    QUARANTINED = "quarantined"


class CapabilityStatus(StrEnum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class HealthObservation:
    """Orthogonal, freshness-aware health state for one bounded component."""

    component: str
    observed_at: datetime
    fresh_until: datetime
    liveness: LivenessStatus
    readiness: ReadinessStatus
    degradation: DegradationStatus
    capability: CapabilityStatus
    reason_code: str
    attributes: TelemetryAttributes = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.component, "health component")
        _validate_identifier(self.reason_code, "health reason code")
        _validate_timestamp(self.observed_at, "health observation time")
        _validate_timestamp(self.fresh_until, "health freshness deadline")
        if self.fresh_until < self.observed_at:
            raise ValueError("health freshness deadline cannot precede observation")
        if not isinstance(self.liveness, LivenessStatus):
            raise TypeError("health liveness must be a LivenessStatus")
        if not isinstance(self.readiness, ReadinessStatus):
            raise TypeError("health readiness must be a ReadinessStatus")
        if not isinstance(self.degradation, DegradationStatus):
            raise TypeError("health degradation must be a DegradationStatus")
        if not isinstance(self.capability, CapabilityStatus):
            raise TypeError("health capability must be a CapabilityStatus")
        object.__setattr__(self, "attributes", tuple(tuple(item) for item in self.attributes))

    @property
    def ready(self) -> bool:
        return (
            self.liveness is LivenessStatus.LIVE
            and self.readiness is ReadinessStatus.READY
            and self.degradation is DegradationStatus.HEALTHY
            and self.capability is CapabilityStatus.AVAILABLE
        )

    def is_stale(self, at: datetime) -> bool:
        _validate_timestamp(at, "health comparison time")
        return at > self.fresh_until


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class SpanStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class LogRecord:
    event_name: str
    level: LogLevel
    observed_at: datetime
    context: TelemetryContext = field(default_factory=TelemetryContext)
    attributes: TelemetryAttributes = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.event_name, "log event name")
        _validate_timestamp(self.observed_at, "log observation time")
        if not isinstance(self.level, LogLevel):
            raise TypeError("log level must be a LogLevel")
        if not isinstance(self.context, TelemetryContext):
            raise TypeError("log context must be a TelemetryContext")
        object.__setattr__(self, "attributes", tuple(tuple(item) for item in self.attributes))


@dataclass(frozen=True, slots=True)
class SpanRecord:
    operation: str
    started_at: datetime
    ended_at: datetime
    status: SpanStatus
    context: TelemetryContext = field(default_factory=TelemetryContext)
    attributes: TelemetryAttributes = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.operation, "span operation")
        _validate_timestamp(self.started_at, "span start time")
        _validate_timestamp(self.ended_at, "span end time")
        if self.ended_at < self.started_at:
            raise ValueError("span end time cannot precede start time")
        if not isinstance(self.status, SpanStatus):
            raise TypeError("span status must be a SpanStatus")
        if not isinstance(self.context, TelemetryContext):
            raise TypeError("span context must be a TelemetryContext")
        object.__setattr__(self, "attributes", tuple(tuple(item) for item in self.attributes))


@dataclass(frozen=True, slots=True)
class MetricPoint:
    name: str
    value: int | float
    unit: str
    observed_at: datetime
    labels: TelemetryAttributes = ()

    def __post_init__(self) -> None:
        _validate_identifier(self.name, "metric name")
        _validate_identifier(self.unit, "metric unit")
        _validate_timestamp(self.observed_at, "metric observation time")
        if type(self.value) not in {int, float} or not math.isfinite(float(self.value)):
            raise ValueError("metric value must be a finite number")
        object.__setattr__(self, "labels", tuple(tuple(item) for item in self.labels))


class UnsafeTelemetryReason(StrEnum):
    TOO_MANY_FIELDS = "too-many-fields"
    INVALID_KEY = "invalid-key"
    DUPLICATE_KEY = "duplicate-key"
    FORBIDDEN_FIELD = "forbidden-field"
    HIGH_CARDINALITY = "high-cardinality"
    INVALID_VALUE = "invalid-value"


class UnsafeTelemetry(ValueError):
    """A telemetry record violated bounded redaction or cardinality policy."""

    def __init__(self, reason: UnsafeTelemetryReason) -> None:
        self.reason = reason
        super().__init__(f"telemetry rejected: {reason.value}")


@dataclass(frozen=True, slots=True)
class TelemetryPolicy:
    max_attributes: int = 32
    max_labels: int = 8
    max_value_length: int = 200
    max_label_length: int = 64

    def __post_init__(self) -> None:
        for value in (
            self.max_attributes,
            self.max_labels,
            self.max_value_length,
            self.max_label_length,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("telemetry policy limits must be positive integers")

    def process(
        self,
        attributes: TelemetryAttributes,
        *,
        labels: bool = False,
    ) -> TelemetryAttributes:
        values = tuple(tuple(item) for item in attributes)
        limit = self.max_labels if labels else self.max_attributes
        if len(values) > limit:
            raise UnsafeTelemetry(UnsafeTelemetryReason.TOO_MANY_FIELDS)
        seen: set[str] = set()
        processed: list[tuple[str, TelemetryScalar]] = []
        for key, value in values:
            if not isinstance(key, str) or _IDENTIFIER.fullmatch(key) is None:
                raise UnsafeTelemetry(UnsafeTelemetryReason.INVALID_KEY)
            if key in seen:
                raise UnsafeTelemetry(UnsafeTelemetryReason.DUPLICATE_KEY)
            seen.add(key)
            fragments = frozenset(key.split("."))
            if fragments & _FORBIDDEN_FRAGMENTS:
                raise UnsafeTelemetry(UnsafeTelemetryReason.FORBIDDEN_FIELD)
            if fragments & _HIGH_CARDINALITY_FRAGMENTS:
                raise UnsafeTelemetry(UnsafeTelemetryReason.HIGH_CARDINALITY)
            if fragments & _SENSITIVE_FRAGMENTS:
                processed.append((key, "[REDACTED]"))
                continue
            processed.append((key, self._validate_value(value, labels=labels)))
        return tuple(processed)

    def _validate_value(self, value: TelemetryScalar, *, labels: bool) -> TelemetryScalar:
        if type(value) not in {str, int, float, bool}:
            raise UnsafeTelemetry(UnsafeTelemetryReason.INVALID_VALUE)
        if isinstance(value, float) and not math.isfinite(value):
            raise UnsafeTelemetry(UnsafeTelemetryReason.INVALID_VALUE)
        if isinstance(value, str):
            limit = self.max_label_length if labels else self.max_value_length
            if not value or len(value) > limit or _CONTROL_CHARACTERS.search(value):
                raise UnsafeTelemetry(UnsafeTelemetryReason.INVALID_VALUE)
            if labels and _IDENTIFIER.fullmatch(value) is None:
                raise UnsafeTelemetry(UnsafeTelemetryReason.HIGH_CARDINALITY)
        return value


class TelemetryExporter(Protocol):
    def export_log(self, record: LogRecord) -> None: ...

    def export_span(self, record: SpanRecord) -> None: ...

    def export_metric(self, point: MetricPoint) -> None: ...

    def export_health(self, observation: HealthObservation) -> None: ...


class NoOpTelemetryExporter:
    def export_log(self, record: LogRecord) -> None:
        del record

    def export_span(self, record: SpanRecord) -> None:
        del record

    def export_metric(self, point: MetricPoint) -> None:
        del point

    def export_health(self, observation: HealthObservation) -> None:
        del observation


@dataclass(frozen=True, slots=True)
class TelemetryDiagnostics:
    exported: int
    dropped: int
    exporter_failures: int


class SafeTelemetry:
    """Validate before export and isolate every optional exporter failure."""

    def __init__(
        self,
        exporters: tuple[TelemetryExporter, ...] = (),
        *,
        policy: TelemetryPolicy | None = None,
    ) -> None:
        self._exporters = tuple(exporters)
        self._policy = policy or TelemetryPolicy()
        self._exported = 0
        self._dropped = 0
        self._exporter_failures = 0
        self._lock = Lock()

    @property
    def diagnostics(self) -> TelemetryDiagnostics:
        with self._lock:
            return TelemetryDiagnostics(
                self._exported,
                self._dropped,
                self._exporter_failures,
            )

    def emit_log(self, record: LogRecord) -> bool:
        if not isinstance(record, LogRecord):
            raise TypeError("log telemetry requires a LogRecord")
        try:
            safe = replace(record, attributes=self._policy.process(record.attributes))
        except UnsafeTelemetry:
            self._increment("dropped")
            return False
        return self._dispatch(lambda exporter: exporter.export_log(safe))

    def emit_span(self, record: SpanRecord) -> bool:
        if not isinstance(record, SpanRecord):
            raise TypeError("span telemetry requires a SpanRecord")
        try:
            safe = replace(record, attributes=self._policy.process(record.attributes))
        except UnsafeTelemetry:
            self._increment("dropped")
            return False
        return self._dispatch(lambda exporter: exporter.export_span(safe))

    def emit_metric(self, point: MetricPoint) -> bool:
        if not isinstance(point, MetricPoint):
            raise TypeError("metric telemetry requires a MetricPoint")
        try:
            safe = replace(point, labels=self._policy.process(point.labels, labels=True))
        except UnsafeTelemetry:
            self._increment("dropped")
            return False
        return self._dispatch(lambda exporter: exporter.export_metric(safe))

    def emit_health(self, observation: HealthObservation) -> bool:
        if not isinstance(observation, HealthObservation):
            raise TypeError("health telemetry requires a HealthObservation")
        try:
            safe = replace(
                observation,
                attributes=self._policy.process(observation.attributes),
            )
        except UnsafeTelemetry:
            self._increment("dropped")
            return False
        return self._dispatch(lambda exporter: exporter.export_health(safe))

    def _dispatch(self, operation: Callable[[TelemetryExporter], None]) -> bool:
        if not self._exporters:
            self._increment("exported")
            return True
        succeeded = False
        for exporter in self._exporters:
            try:
                operation(exporter)
            except Exception:
                self._increment("exporter_failures")
            else:
                succeeded = True
                self._increment("exported")
        return succeeded

    def _increment(self, counter: str) -> None:
        with self._lock:
            if counter == "exported":
                self._exported += 1
            elif counter == "dropped":
                self._dropped += 1
            elif counter == "exporter_failures":
                self._exporter_failures += 1
            else:  # pragma: no cover - private invariant
                raise RuntimeError("unknown telemetry diagnostic counter")
