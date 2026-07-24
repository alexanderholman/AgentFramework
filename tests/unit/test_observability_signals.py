from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from af.observability import (
    CapabilityState,
    DegradationState,
    HealthState,
    LivenessState,
    ReadinessState,
    TelemetryContext,
    TelemetryKind,
    TelemetryPublisher,
    TelemetrySignal,
)

_NOW = datetime(2026, 7, 24, tzinfo=UTC)


def _ready_health() -> HealthState:
    return HealthState(
        liveness=LivenessState.LIVE,
        readiness=ReadinessState.READY,
        degradation=DegradationState.HEALTHY,
        capability=CapabilityState.AVAILABLE,
        reason_code="runtime.ready",
        fresh_until=_NOW + timedelta(seconds=30),
    )


def test_signal_snapshots_safe_attributes_and_redacts_diagnostics() -> None:
    signal = TelemetrySignal.create(
        kind=TelemetryKind.SPAN,
        name="bus.dispatch",
        observed_at=_NOW,
        context=TelemetryContext(operation="bus.dispatch", correlation_id="private-id"),
        attributes={"driver": "sqlite", "attempt": 2, "cached": False},
    )

    assert signal.attributes == (
        ("attempt", 2),
        ("cached", False),
        ("driver", "sqlite"),
    )
    assert signal.observed_at is _NOW
    rendered = repr(signal)
    assert "sqlite" not in rendered
    assert "private-id" not in rendered
    assert "[REDACTED]" in rendered


def test_health_signal_keeps_health_dimensions_and_freshness_separate() -> None:
    state = HealthState(
        liveness=LivenessState.LIVE,
        readiness=ReadinessState.WARMING,
        degradation=DegradationState.QUARANTINED,
        capability=CapabilityState.MISMATCH,
        reason_code="runtime.restart-limit",
        fresh_until=_NOW + timedelta(seconds=30),
    )
    signal = TelemetrySignal.health(
        name="database.primary",
        observed_at=_NOW,
        state=state,
        attributes={"capability": "savepoints"},
    )

    assert signal.kind is TelemetryKind.HEALTH
    assert signal.health_state is state
    assert state.liveness is LivenessState.LIVE
    assert state.readiness is ReadinessState.WARMING
    assert state.degradation is DegradationState.QUARANTINED
    assert state.capability is CapabilityState.MISMATCH
    assert state.ready is False
    assert state.is_stale(_NOW + timedelta(seconds=29)) is False
    assert state.is_stale(_NOW + timedelta(seconds=31)) is True
    assert signal.attributes == (("capability", "savepoints"),)


def test_health_ready_requires_every_dimension_and_fresh_evidence() -> None:
    state = _ready_health()
    signal = TelemetrySignal.health(
        name="runtime.local",
        observed_at=_NOW,
        state=state,
    )

    assert state.ready is True
    assert state.is_stale(_NOW) is False
    assert signal.observed_at == _NOW

    with pytest.raises(ValueError, match="cannot precede"):
        TelemetrySignal.health(
            name="runtime.local",
            observed_at=_NOW,
            state=HealthState(
                liveness=LivenessState.LIVE,
                readiness=ReadinessState.READY,
                degradation=DegradationState.HEALTHY,
                capability=CapabilityState.AVAILABLE,
                reason_code="runtime.ready",
                fresh_until=_NOW - timedelta(seconds=1),
            ),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        state.is_stale(datetime(2026, 7, 24))


@pytest.mark.parametrize(
    ("attributes", "message"),
    [
        ({"password": "private"}, "sensitive"),
        ({"embedding_vector": "private"}, "sensitive"),
        ({"result_content": "private"}, "sensitive"),
        ({"driver": "contains spaces"}, "low-cardinality"),
        ({"driver": float("inf")}, "finite"),
        ({"driver": 2**63}, "signed 64-bit"),
        ({"a": "x", "b": "x", "c": "x"}, "at most 2"),
    ],
)
def test_signal_rejects_sensitive_or_unbounded_attributes(
    attributes: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        TelemetrySignal.create(
            kind=TelemetryKind.LOG,
            name="test.signal",
            observed_at=_NOW,
            attributes=attributes,
            attribute_limit=2,
        )


def test_signal_requires_event_time_and_direct_construction_cannot_bypass_validation() -> None:
    with pytest.raises(ValueError, match="observation time"):
        TelemetrySignal.create(
            kind=TelemetryKind.LOG,
            name="test.signal",
            observed_at=datetime(2026, 7, 24),
        )
    with pytest.raises(ValueError, match="unsafe"):
        TelemetrySignal(
            kind=TelemetryKind.LOG,
            name="test.signal",
            observed_at=_NOW,
            context=None,
            attributes=(("password", "private"),),
        )
    with pytest.raises(ValueError, match="health state"):
        TelemetrySignal(
            kind=TelemetryKind.HEALTH,
            name="test.health",
            observed_at=_NOW,
            context=None,
            attributes=(),
        )
    with pytest.raises(ValueError, match="only valid"):
        TelemetrySignal(
            kind=TelemetryKind.LOG,
            name="test.signal",
            observed_at=_NOW,
            context=None,
            attributes=(),
            health_state=_ready_health(),
        )


def test_publisher_isolates_sink_failure_from_application_semantics() -> None:
    @dataclass
    class RecordingSink:
        signals: list[TelemetrySignal] = field(default_factory=list)

        def emit(self, signal: TelemetrySignal, /) -> None:
            self.signals.append(signal)

    class BrokenSink:
        def emit(self, _signal: TelemetrySignal, /) -> None:
            raise RuntimeError("private sink failure")

    recording = RecordingSink()
    publisher = TelemetryPublisher((BrokenSink(), recording))
    signal = TelemetrySignal.create(
        kind=TelemetryKind.LIFECYCLE,
        name="application.bootstrap",
        observed_at=_NOW,
    )

    def application_operation() -> int:
        publisher.publish(signal)
        return 42

    assert application_operation() == 42
    assert recording.signals == [signal]
    assert publisher.evidence.accepted_signals == 1
    assert publisher.evidence.successful_deliveries == 1
    assert publisher.evidence.failed_deliveries == 1
    assert "private sink failure" not in repr(publisher.evidence)


def test_publisher_bounds_cross_signal_label_cardinality() -> None:
    class Sink:
        def __init__(self) -> None:
            self.count = 0

        def emit(self, _signal: TelemetrySignal, /) -> None:
            self.count += 1

    sink = Sink()
    publisher = TelemetryPublisher((sink,), label_value_limit=2)

    for driver in ("sqlite", "postgres", "mysql", "sqlite"):
        publisher.publish(
            TelemetrySignal.create(
                kind=TelemetryKind.METRIC,
                name="database.query",
                observed_at=_NOW,
                attributes={"driver": driver},
            )
        )

    assert sink.count == 3
    assert publisher.evidence.accepted_signals == 3
    assert publisher.evidence.cardinality_drops == 1


def test_publisher_bounds_the_number_of_label_series() -> None:
    class Sink:
        def __init__(self) -> None:
            self.count = 0

        def emit(self, _signal: TelemetrySignal, /) -> None:
            self.count += 1

    sink = Sink()
    publisher = TelemetryPublisher((sink,), label_series_limit=2)

    for name in ("one.signal", "two.signal", "three.signal"):
        publisher.publish(
            TelemetrySignal.create(
                kind=TelemetryKind.METRIC,
                name=name,
                observed_at=_NOW,
                attributes={"driver": "sqlite"},
            )
        )

    assert sink.count == 2
    assert publisher.evidence.cardinality_drops == 1


def test_publisher_rejects_async_sinks_before_use() -> None:
    class AsyncSink:
        async def emit(self, _signal: TelemetrySignal, /) -> None:
            return None

    with pytest.raises(ValueError, match="synchronous"):
        TelemetryPublisher((AsyncSink(),))
