from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from af.framework.context import (
    AcceptanceScope,
    CancellationToken,
    ExecutionContext,
    Principal,
    TraceContext,
)
from af.observability import (
    CapabilityStatus,
    DegradationStatus,
    HealthObservation,
    LivenessStatus,
    LogLevel,
    LogRecord,
    MetricPoint,
    NoOpTelemetryExporter,
    ReadinessStatus,
    SafeTelemetry,
    SpanRecord,
    SpanStatus,
    TelemetryContext,
    TelemetryPolicy,
    UnsafeTelemetry,
    UnsafeTelemetryReason,
)

_NOW = datetime(2026, 7, 24, tzinfo=UTC)


def _health(*, readiness: ReadinessStatus = ReadinessStatus.READY) -> HealthObservation:
    return HealthObservation(
        component="runtime.local",
        observed_at=_NOW,
        fresh_until=_NOW + timedelta(seconds=30),
        liveness=LivenessStatus.LIVE,
        readiness=readiness,
        degradation=DegradationStatus.HEALTHY,
        capability=CapabilityStatus.AVAILABLE,
        reason_code="runtime.ready",
        attributes=(("runtime.digest", "sha256-safe"),),
    )


def test_telemetry_context_round_trips_without_principal_or_raw_tenant_identity() -> None:
    cancellation = CancellationToken()
    principal = Principal("private-subject", "user", roles=("operator",))
    source = ExecutionContext(
        principal=principal,
        tenant_id="private-tenant",
        correlation_id="correlation-1",
        causation_id="causation-1",
        deadline=_NOW + timedelta(minutes=1),
        trace=TraceContext("trace-1", "span-1", 1),
        locale="en-GB",
        requested_scope=AcceptanceScope.REVIEWED_GENERATIVE,
        effective_scope=AcceptanceScope.DETERMINISTIC,
        cancellation=cancellation,
    )

    context = TelemetryContext.from_execution_context(
        source,
        operation_id="task.execute",
        tenant_scope="tenant-class-a",
    )
    carrier = {"external.header": "kept"}
    context.inject(carrier)
    restored = TelemetryContext.extract(carrier)
    applied = restored.to_execution_context(source)

    assert restored == context
    assert carrier["external.header"] == "kept"
    assert "private-subject" not in repr(context)
    assert "private-tenant" not in carrier.values()
    assert applied.principal is principal
    assert applied.tenant_id == "private-tenant"
    assert applied.correlation_id == "correlation-1"
    assert applied.causation_id == "causation-1"
    assert applied.trace == TraceContext("trace-1", "span-1", 1)
    assert applied.deadline == source.deadline
    assert applied.locale == "en-GB"
    assert applied.requested_scope is AcceptanceScope.REVIEWED_GENERATIVE
    assert applied.effective_scope is AcceptanceScope.DETERMINISTIC
    assert applied.cancellation is cancellation


def test_telemetry_context_supports_empty_context_and_rejects_invalid_carriers() -> None:
    empty = TelemetryContext.from_execution_context(ExecutionContext())
    carrier: dict[str, str] = {}
    empty.inject(carrier)
    assert carrier == {"af.context.version": "1"}
    restored = TelemetryContext.extract(carrier).to_execution_context()
    assert restored.correlation_id is None
    assert restored.causation_id is None
    assert restored.trace is None

    with pytest.raises(ValueError, match="span id"):
        TelemetryContext(span_id="orphan")
    with pytest.raises(ValueError, match="trace flags"):
        TelemetryContext(trace_flags=256)
    with pytest.raises(TypeError, match="ExecutionContext"):
        TelemetryContext.from_execution_context(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="template"):
        empty.to_execution_context(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unsupported AF"):
        empty.inject({"af.context.future": "value"})
    with pytest.raises(ValueError, match="unsupported AF"):
        TelemetryContext.extract(
            {"af.context.version": "1", "af.context.future": "value"}
        )
    with pytest.raises(ValueError, match="version"):
        TelemetryContext.extract({})
    with pytest.raises(ValueError, match="trace flags"):
        TelemetryContext.extract(
            {"af.context.version": "1", "af.context.trace_flags": "not-a-number"}
        )
    with pytest.raises(TypeError, match="strings"):
        TelemetryContext.extract(
            cast(
                dict[str, str],
                {"af.context.version": "1", "af.context.trace_id": 7},
            )
        )


def test_health_dimensions_are_independent_and_freshness_is_explicit() -> None:
    ready = _health()
    warming = _health(readiness=ReadinessStatus.WARMING)
    mismatch = HealthObservation(
        component="runtime.local",
        observed_at=_NOW,
        fresh_until=_NOW,
        liveness=LivenessStatus.LIVE,
        readiness=ReadinessStatus.NOT_READY,
        degradation=DegradationStatus.DEGRADED,
        capability=CapabilityStatus.MISMATCH,
        reason_code="runtime.capability-mismatch",
    )

    assert ready.ready is True
    assert warming.ready is False
    assert mismatch.liveness is LivenessStatus.LIVE
    assert mismatch.capability is CapabilityStatus.MISMATCH
    assert ready.is_stale(_NOW + timedelta(seconds=29)) is False
    assert ready.is_stale(_NOW + timedelta(seconds=31)) is True

    with pytest.raises(ValueError, match="precede"):
        HealthObservation(
            component="runtime.local",
            observed_at=_NOW,
            fresh_until=_NOW - timedelta(seconds=1),
            liveness=LivenessStatus.LIVE,
            readiness=ReadinessStatus.READY,
            degradation=DegradationStatus.HEALTHY,
            capability=CapabilityStatus.AVAILABLE,
            reason_code="runtime.ready",
        )
    with pytest.raises(ValueError, match="timezone"):
        ready.is_stale(datetime(2026, 7, 24))


def test_telemetry_policy_redacts_secrets_and_rejects_unsafe_shape() -> None:
    policy = TelemetryPolicy(max_attributes=2, max_labels=1)
    assert policy.process(
        (("service.name", "broker"), ("auth.token", "private"))
    ) == (("service.name", "broker"), ("auth.token", "[REDACTED]"))
    assert policy.process((("status", "ready"),), labels=True) == (("status", "ready"),)

    cases = (
        ((("prompt", "private"),), UnsafeTelemetryReason.FORBIDDEN_FIELD, False),
        ((("trace_id", "trace"),), UnsafeTelemetryReason.HIGH_CARDINALITY, False),
        ((("bad key", "value"),), UnsafeTelemetryReason.INVALID_KEY, False),
        (
            (("service.name", "one"), ("service.name", "two")),
            UnsafeTelemetryReason.DUPLICATE_KEY,
            False,
        ),
        (
            (("one", 1), ("two", 2), ("three", 3)),
            UnsafeTelemetryReason.TOO_MANY_FIELDS,
            False,
        ),
        ((("status", "Not Ready"),), UnsafeTelemetryReason.HIGH_CARDINALITY, True),
        ((("status", float("inf")),), UnsafeTelemetryReason.INVALID_VALUE, False),
        ((("status", ""),), UnsafeTelemetryReason.INVALID_VALUE, False),
        (
            cast(tuple[tuple[str, str | int | float | bool], ...], (("status", object()),)),
            UnsafeTelemetryReason.INVALID_VALUE,
            False,
        ),
    )
    for attributes, reason, labels in cases:
        with pytest.raises(UnsafeTelemetry) as captured:
            policy.process(attributes, labels=labels)
        assert captured.value.reason is reason
        assert "private" not in str(captured.value)

    with pytest.raises(ValueError, match="positive integers"):
        TelemetryPolicy(max_attributes=0)


def test_records_validate_timestamps_identifiers_and_finite_metric_values() -> None:
    context = TelemetryContext(trace_id="trace", span_id="span")
    log = LogRecord("request.accepted", LogLevel.INFO, _NOW, context)
    span = SpanRecord("request.dispatch", _NOW, _NOW, SpanStatus.OK, context)
    point = MetricPoint("request.count", 1, "count", _NOW)
    assert log.context is context
    assert span.status is SpanStatus.OK
    assert point.value == 1

    with pytest.raises(ValueError, match="precede"):
        SpanRecord(
            "request.dispatch",
            _NOW,
            _NOW - timedelta(seconds=1),
            SpanStatus.ERROR,
        )
    with pytest.raises(ValueError, match="finite"):
        MetricPoint("request.count", float("nan"), "count", _NOW)
    with pytest.raises(ValueError, match="identifier"):
        LogRecord("Request Accepted", LogLevel.INFO, _NOW)


@dataclass(slots=True)
class _RecordingExporter:
    logs: list[LogRecord]
    spans: list[SpanRecord]
    metrics: list[MetricPoint]
    health: list[HealthObservation]

    @classmethod
    def create(cls) -> _RecordingExporter:
        return cls([], [], [], [])

    def export_log(self, record: LogRecord) -> None:
        self.logs.append(record)

    def export_span(self, record: SpanRecord) -> None:
        self.spans.append(record)

    def export_metric(self, point: MetricPoint) -> None:
        self.metrics.append(point)

    def export_health(self, observation: HealthObservation) -> None:
        self.health.append(observation)


class _FailingExporter(NoOpTelemetryExporter):
    def export_log(self, record: LogRecord) -> None:
        del record
        raise RuntimeError("private exporter failure")

    def export_span(self, record: SpanRecord) -> None:
        del record
        raise RuntimeError("private exporter failure")

    def export_metric(self, point: MetricPoint) -> None:
        del point
        raise RuntimeError("private exporter failure")

    def export_health(self, observation: HealthObservation) -> None:
        del observation
        raise RuntimeError("private exporter failure")


def test_safe_telemetry_isolates_exporter_failure_and_exports_sanitized_records() -> None:
    recorder = _RecordingExporter.create()
    telemetry = SafeTelemetry((_FailingExporter(), recorder))
    context = TelemetryContext(operation_id="request.dispatch")

    assert telemetry.emit_log(
        LogRecord(
            "request.accepted",
            LogLevel.INFO,
            _NOW,
            context,
            (("auth.password", "private"),),
        )
    )
    assert telemetry.emit_span(
        SpanRecord("request.dispatch", _NOW, _NOW, SpanStatus.OK, context)
    )
    assert telemetry.emit_metric(
        MetricPoint("request.count", 1, "count", _NOW, (("status", "ready"),))
    )
    assert telemetry.emit_health(_health())

    assert recorder.logs[0].attributes == (("auth.password", "[REDACTED]"),)
    assert len(recorder.spans) == len(recorder.metrics) == len(recorder.health) == 1
    assert telemetry.diagnostics.exported == 4
    assert telemetry.diagnostics.exporter_failures == 4
    assert telemetry.diagnostics.dropped == 0


def test_safe_telemetry_drops_unsafe_records_and_supports_noop_operation() -> None:
    recorder = _RecordingExporter.create()
    telemetry = SafeTelemetry((recorder,))
    assert telemetry.emit_log(
        LogRecord("request.accepted", LogLevel.INFO, _NOW, attributes=(("prompt", "x"),))
    ) is False
    assert recorder.logs == []
    assert telemetry.diagnostics.dropped == 1

    failing = SafeTelemetry((_FailingExporter(),))
    assert failing.emit_log(LogRecord("request.accepted", LogLevel.INFO, _NOW)) is False
    assert failing.diagnostics.exporter_failures == 1

    noop = SafeTelemetry()
    assert noop.emit_health(_health()) is True
    assert noop.diagnostics.exported == 1

    exporter = NoOpTelemetryExporter()
    exporter.export_log(LogRecord("request.accepted", LogLevel.INFO, _NOW))
    exporter.export_span(SpanRecord("request.dispatch", _NOW, _NOW, SpanStatus.OK))
    exporter.export_metric(MetricPoint("request.count", 1, "count", _NOW))
    exporter.export_health(_health())

    with pytest.raises(TypeError, match="LogRecord"):
        noop.emit_log(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="SpanRecord"):
        noop.emit_span(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MetricPoint"):
        noop.emit_metric(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="HealthObservation"):
        noop.emit_health(object())  # type: ignore[arg-type]
