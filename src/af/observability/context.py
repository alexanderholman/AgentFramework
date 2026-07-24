"""Versioned, tenant-safe telemetry context propagation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from af.framework.context import ExecutionContext

__all__ = ("TelemetryContext",)

_VERSION = "af.telemetry.v1"
_OPERATION = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_TENANT_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_VALUE_LENGTH = 200
_MAX_FIELDS = 8
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_FIELDS = frozenset(
    {
        "version",
        "operation",
        "correlation_id",
        "causation_id",
        "trace_id",
        "span_id",
        "trace_flags",
        "tenant_ref",
    }
)


def _opaque(value: object, label: str) -> str | None:
    if value is None:
        return None
    if (
        type(value) is not str
        or not value
        or len(value) > _MAX_VALUE_LENGTH
        or _UNSAFE_TEXT.search(value)
    ):
        raise ValueError(f"{label} must be bounded non-empty single-line text")
    return value


@dataclass(frozen=True, slots=True, repr=False)
class TelemetryContext:
    """Minimal metadata safe to carry across telemetry and process boundaries."""

    operation: str
    correlation_id: str | None = None
    causation_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    trace_flags: int = 0
    tenant_ref: str | None = None

    def __post_init__(self) -> None:
        if type(self.operation) is not str or _OPERATION.fullmatch(self.operation) is None:
            raise ValueError("telemetry operation must be a canonical identifier")
        for value, label in (
            (self.correlation_id, "telemetry correlation id"),
            (self.causation_id, "telemetry causation id"),
            (self.trace_id, "telemetry trace id"),
            (self.span_id, "telemetry span id"),
        ):
            _opaque(value, label)
        if type(self.trace_flags) is not int or not 0 <= self.trace_flags <= 255:
            raise ValueError("telemetry trace flags must be an unsigned byte")
        if self.trace_id is None and (self.span_id is not None or self.trace_flags):
            raise ValueError("telemetry span and flags require a trace id")
        if self.tenant_ref is not None and (
            type(self.tenant_ref) is not str or _TENANT_REF.fullmatch(self.tenant_ref) is None
        ):
            raise ValueError("telemetry tenant reference must be an opaque sha256 digest")

    @classmethod
    def from_execution(
        cls,
        context: ExecutionContext,
        *,
        operation: str,
        tenant_ref: str | None = None,
    ) -> TelemetryContext:
        """Project safe metadata without copying principal or raw tenant identity."""

        if not isinstance(context, ExecutionContext):
            raise TypeError("telemetry projection requires an ExecutionContext")
        trace = context.trace
        return cls(
            operation=operation,
            correlation_id=context.correlation_id,
            causation_id=context.causation_id,
            trace_id=None if trace is None else trace.trace_id,
            span_id=None if trace is None else trace.span_id,
            trace_flags=0 if trace is None else trace.trace_flags,
            tenant_ref=tenant_ref,
        )

    def to_envelope(self) -> tuple[tuple[str, str], ...]:
        """Return a deterministic string-only process-boundary envelope."""

        fields = (
            ("version", _VERSION),
            ("operation", self.operation),
            ("correlation_id", self.correlation_id),
            ("causation_id", self.causation_id),
            ("trace_id", self.trace_id),
            ("span_id", self.span_id),
            ("trace_flags", str(self.trace_flags) if self.trace_id is not None else None),
            ("tenant_ref", self.tenant_ref),
        )
        return tuple((key, value) for key, value in fields if value is not None)

    @classmethod
    def from_envelope(cls, envelope: Mapping[str, str]) -> TelemetryContext:
        """Validate and restore a process-boundary telemetry envelope."""

        if not isinstance(envelope, Mapping):
            raise TypeError("telemetry envelope must be a mapping")
        if len(envelope) > _MAX_FIELDS or any(type(key) is not str for key in envelope):
            raise ValueError("telemetry envelope fields are invalid")
        unknown = set(envelope) - _FIELDS
        if unknown:
            raise ValueError("telemetry envelope contains unknown fields")
        if envelope.get("version") != _VERSION:
            raise ValueError("telemetry envelope version is unsupported")
        if any(type(value) is not str for value in envelope.values()):
            raise ValueError("telemetry envelope values must be strings")
        raw_flags = envelope.get("trace_flags", "0")
        if not raw_flags.isascii() or not raw_flags.isdecimal():
            raise ValueError("telemetry envelope trace flags are invalid")
        return cls(
            operation=envelope.get("operation", ""),
            correlation_id=envelope.get("correlation_id"),
            causation_id=envelope.get("causation_id"),
            trace_id=envelope.get("trace_id"),
            span_id=envelope.get("span_id"),
            trace_flags=int(raw_flags),
            tenant_ref=envelope.get("tenant_ref"),
        )

    def __repr__(self) -> str:
        return (
            f"TelemetryContext(operation={self.operation!r}, "
            "correlation_id='[REDACTED]', causation_id='[REDACTED]', "
            "trace='[REDACTED]', tenant_ref='[REDACTED]')"
        )
