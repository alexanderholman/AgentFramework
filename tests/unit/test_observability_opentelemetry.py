from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest

from af.observability import (
    CapabilityState,
    DegradationState,
    HealthState,
    LivenessState,
    OpenTelemetryRecord,
    OpenTelemetrySink,
    ReadinessState,
    TelemetryContext,
    TelemetryKind,
    TelemetryPublisher,
    TelemetrySignal,
)

_NOW = datetime(2026, 7, 24, 1, 2, 3, 456789, tzinfo=UTC)


def _health() -> HealthState:
    return HealthState(
        liveness=LivenessState.LIVE,
        readiness=ReadinessState.WARMING,
        degradation=DegradationState.DEGRADED,
        capability=CapabilityState.MISMATCH,
        reason_code="runtime.warming",
        fresh_until=_NOW + timedelta(seconds=30),
    )


def _record() -> OpenTelemetryRecord:
    signal = TelemetrySignal.create(
        kind=TelemetryKind.LOG,
        name="test.signal",
        observed_at=_NOW,
    )
    return OpenTelemetryRecord.from_signal(signal)


def test_record_preserves_safe_context_and_exact_event_time() -> None:
    context = TelemetryContext(
        operation="application.bootstrap",
        correlation_id="correlation",
        causation_id="causation",
        trace_id="trace",
        span_id="span",
        trace_flags=1,
        tenant_ref="sha256:" + "a" * 64,
    )
    signal = TelemetrySignal.create(
        kind=TelemetryKind.LIFECYCLE,
        name="application.bootstrap",
        observed_at=_NOW,
        context=context,
        attributes={"state": "booting", "attempt": 1},
    )

    record = OpenTelemetryRecord.from_signal(signal, instrumentation_scope="af.framework")

    assert record.schema_version == "af.opentelemetry.v1"
    assert record.instrumentation_scope == "af.framework"
    assert record.kind is TelemetryKind.LIFECYCLE
    assert record.name == "application.bootstrap"
    assert record.time_unix_nano % 1_000_000_000 == 456_789_000
    assert record.attributes == (
        ("af.attribute.attempt", 1),
        ("af.attribute.state", "booting"),
    )
    assert record.operation == "application.bootstrap"
    assert record.correlation_id == "correlation"
    assert record.causation_id == "causation"
    assert record.trace_id == "trace"
    assert record.span_id == "span"
    assert record.trace_flags == 1
    assert record.tenant_ref == "sha256:" + "a" * 64
    rendered = repr(record)
    assert "correlation" not in rendered
    assert "booting" not in rendered
    assert "[REDACTED]" in rendered


def test_record_preserves_orthogonal_health_dimensions() -> None:
    signal = TelemetrySignal.health(
        name="runtime.local",
        observed_at=_NOW,
        state=_health(),
        attributes={"driver": "llama-cpp"},
    )

    record = OpenTelemetryRecord.from_signal(signal)

    assert record.attributes == (
        ("af.attribute.driver", "llama-cpp"),
        ("af.health.capability", "mismatch"),
        ("af.health.degradation", "degraded"),
        (
            "af.health.fresh_until_unix_nano",
            record.time_unix_nano + 30_000_000_000,
        ),
        ("af.health.liveness", "live"),
        ("af.health.readiness", "warming"),
        ("af.health.reason_code", "runtime.warming"),
    )


def test_sink_exports_one_dependency_free_record() -> None:
    @dataclass
    class Exporter:
        records: list[OpenTelemetryRecord] = field(default_factory=list)

        def export(self, record: OpenTelemetryRecord, /) -> None:
            self.records.append(record)

    exporter = Exporter()
    sink = OpenTelemetrySink(exporter, instrumentation_scope="af.test")
    signal = TelemetrySignal.create(
        kind=TelemetryKind.LOG,
        name="test.signal",
        observed_at=_NOW,
    )

    sink.emit(signal)

    assert len(exporter.records) == 1
    assert exporter.records[0].instrumentation_scope == "af.test"


def test_sink_rejects_async_exporters_and_async_results() -> None:
    class AsyncExporter:
        async def export(self, _record: OpenTelemetryRecord, /) -> None:
            return None

    with pytest.raises(ValueError, match="synchronous"):
        OpenTelemetrySink(AsyncExporter())

    class AwaitableExporter:
        def export(self, _record: OpenTelemetryRecord, /) -> object:
            async def result() -> None:
                return None

            return result()

    sink = OpenTelemetrySink(AwaitableExporter())
    signal = TelemetrySignal.create(
        kind=TelemetryKind.LOG,
        name="test.signal",
        observed_at=_NOW,
    )
    with pytest.raises(TypeError, match="asynchronous result"):
        sink.emit(signal)


def test_publisher_isolates_optional_exporter_failure() -> None:
    class BrokenExporter:
        def export(self, _record: OpenTelemetryRecord, /) -> None:
            raise RuntimeError("export failed")

    @dataclass
    class RecordingSink:
        signals: list[TelemetrySignal] = field(default_factory=list)

        def emit(self, signal: TelemetrySignal, /) -> None:
            self.signals.append(signal)

    recording = RecordingSink()
    publisher = TelemetryPublisher((OpenTelemetrySink(BrokenExporter()), recording))
    signal = TelemetrySignal.create(
        kind=TelemetryKind.SPAN,
        name="application.dispatch",
        observed_at=_NOW,
    )

    publisher.publish(signal)

    assert recording.signals == [signal]
    assert publisher.evidence.accepted_signals == 1
    assert publisher.evidence.failed_deliveries == 1
    assert publisher.evidence.successful_deliveries == 1


def test_projection_rejects_invalid_inputs() -> None:
    with pytest.raises(TypeError, match="TelemetrySignal"):
        OpenTelemetryRecord.from_signal(object())  # type: ignore[arg-type]

    signal = TelemetrySignal.create(
        kind=TelemetryKind.LOG,
        name="test.signal",
        observed_at=_NOW,
    )
    with pytest.raises(ValueError, match="scope"):
        OpenTelemetryRecord.from_signal(signal, instrumentation_scope="not a scope")


def test_direct_construction_cannot_bypass_record_validation() -> None:
    record = _record()

    with pytest.raises(ValueError, match="schema version"):
        replace(record, schema_version="other")
    with pytest.raises(ValueError, match="non-negative"):
        replace(record, time_unix_nano=-1)
    with pytest.raises(ValueError, match="sensitive"):
        replace(record, attributes=(("af.attribute.password", "private"),))
    with pytest.raises(ValueError, match="canonically sorted"):
        replace(
            record,
            attributes=(
                ("af.attribute.state", "ready"),
                ("af.attribute.attempt", 1),
            ),
        )
    with pytest.raises(ValueError, match="trace id"):
        replace(record, span_id="span")


def test_projection_rejects_pre_epoch_events() -> None:
    signal = TelemetrySignal.create(
        kind=TelemetryKind.LOG,
        name="test.signal",
        observed_at=datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="Unix epoch"):
        OpenTelemetryRecord.from_signal(signal)
