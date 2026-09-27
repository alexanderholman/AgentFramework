"""Optional observability is bounded, tenant-scoped and redacted before persistence."""

from pathlib import Path

import pytest

from af.application.observability_products import InspectorService, MetricsService
from af.framework.context import Principal
from af.framework.observability_products import (
    InspectorKind,
    InspectorPolicy,
    MetricDefinition,
    MetricKind,
    ObservabilityDenied,
    ObservabilityUnavailable,
)
from af.infrastructure.observability_products import SQLiteInspectorStore, SQLiteMetricsStore


def test_inspector_redacts_samples_retains_and_authorizes(tmp_path: Path) -> None:
    now = [100.0]
    path = tmp_path / "inspector.sqlite3"
    store = SQLiteInspectorStore(path)
    policy = InspectorPolicy(
        names=("http.request",), tags=("failure",), max_records=2, retention_seconds=60
    )
    inspector = InspectorService(store, policy, tenant_id="a", clock=lambda: now[0])
    principal = Principal(
        "operator", "user", scopes=("af:observability:read", "af:observability:write")
    )
    for index in range(3):
        assert inspector.collect(
            InspectorKind.REQUEST,
            "http.request",
            event_id=str(index),
            fields={
                "method": "GET",
                "status": 200,
                "duration_ms": 2.5,
                "authorization": "Bearer private-secret",
                "payload": {"password": "nested-secret"},
            },
            tags=("failure",),
            correlation_id="private-request-id",
        )
    rows = inspector.entries(principal)
    assert len(rows) == 2
    assert dict(rows[0].attributes)["redacted_fields"] == 2
    assert rows[0].correlation_ref.startswith("sha256:")
    assert b"private-secret" not in path.read_bytes()
    assert b"nested-secret" not in path.read_bytes()
    assert b"private-request-id" not in path.read_bytes()
    with pytest.raises(ObservabilityDenied):
        inspector.entries(Principal("guest", "user"))
    other = InspectorService(store, policy, tenant_id="b", clock=lambda: now[0])
    assert other.entries(principal) == ()
    now[0] = 200
    assert inspector.entries(principal) == ()
    assert inspector.purge(principal) == 2


def test_metrics_are_independent_and_enforce_registered_label_domains(tmp_path: Path) -> None:
    definition = MetricDefinition("requests", MetricKind.COUNTER, (("outcome", ("ok", "error")),))
    histogram = MetricDefinition("latency", MetricKind.HISTOGRAM, (), (10.0, 100.0))
    service = MetricsService(
        SQLiteMetricsStore(tmp_path / "metrics.sqlite3"),
        (definition, histogram),
        tenant_id="a",
        clock=lambda: 100,
    )
    principal = Principal("operator", "user", scopes=("af:observability:read",))
    assert service.record("requests", 1, {"outcome": "ok"})
    assert service.record("requests", 2, {"outcome": "ok"})
    assert not service.record("requests", 1, {"outcome": "user-999"})
    assert not service.record("requests", -1, {"outcome": "ok"})
    for value in (5, 50, 200):
        assert service.record("latency", value, {})
    cards = service.cards(principal)
    assert next(row for row in cards if row.name == "requests").total == 3
    assert next(row for row in cards if row.name == "latency").buckets == (1, 1, 1)
    with pytest.raises(ObservabilityDenied):
        service.cards(Principal("guest", "user"))


def test_optional_collector_failures_sampling_filters_and_restart(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from af.infrastructure.observability_products import (
        InspectorTelemetrySink,
        MetricsTelemetrySink,
    )
    from af.observability.context import TelemetryContext
    from af.observability.signals import TelemetryKind, TelemetryPublisher, TelemetrySignal

    now = [100.0]
    path = tmp_path / "observations"
    policy = InspectorPolicy(("request.completed",), ("failure",), sample_rate=0)
    store = SQLiteInspectorStore(path)
    inspector = InspectorService(store, policy, tenant_id="a", clock=lambda: now[0])
    assert not inspector.collect(
        InspectorKind.REQUEST, "request.completed", event_id="x", fields={}
    )
    policy = InspectorPolicy(("request.completed",), ("failure",))
    inspector = InspectorService(store, policy, tenant_id="a", clock=lambda: now[0])
    assert not inspector.collect(InspectorKind.REQUEST, "unknown", event_id="x", fields={})
    assert not inspector.collect(
        InspectorKind.REQUEST, "request.completed", event_id="x", fields={}, tags=("raw-secret",)
    )
    assert not inspector.collect(
        InspectorKind.REQUEST,
        "request.completed",
        event_id="x",
        fields={str(index): index for index in range(65)},
    )
    assert inspector.collect(
        InspectorKind.QUERY,
        "request.completed",
        event_id="x",
        fields={"operation": "request.completed", "status": "ok"},
        tags=("failure",),
    )
    actor = Principal("op", "user", scopes=("af:observability:read", "af:observability:write"))
    restarted = InspectorService(
        SQLiteInspectorStore(path), policy, tenant_id="a", clock=lambda: 100
    )
    assert (
        len(
            restarted.entries(
                actor, kind=InspectorKind.QUERY, name="request.completed", tag="failure"
            )
        )
        == 1
    )
    with pytest.raises(ValueError):
        restarted.entries(actor, limit=101)
    with pytest.raises(ObservabilityDenied):
        restarted.entries(Principal("guest", "user"))
    definition = MetricDefinition("requests", MetricKind.COUNTER)
    metrics = MetricsService(
        SQLiteMetricsStore(path), (definition,), tenant_id="a", clock=lambda: now[0]
    )
    telemetry = TelemetryPublisher(
        (InspectorTelemetrySink(inspector), MetricsTelemetrySink(metrics))
    )
    telemetry.publish(
        TelemetrySignal.create(
            kind=TelemetryKind.LOG,
            name="request.completed",
            observed_at=datetime.now(UTC),
            attributes={"status": "ok"},
        )
    )
    telemetry.publish(
        TelemetrySignal.create(
            kind=TelemetryKind.METRIC,
            name="requests",
            observed_at=datetime.now(UTC),
            attributes={"value": 1},
        )
    )
    foreign = TelemetryContext("test", tenant_ref="sha256:" + "b" * 64)
    telemetry.publish(
        TelemetrySignal.create(
            kind=TelemetryKind.METRIC,
            name="requests",
            observed_at=datetime.now(UTC),
            context=foreign,
            attributes={"value": 999},
        )
    )
    telemetry.publish(
        TelemetrySignal.create(
            kind=TelemetryKind.LOG,
            name="request.completed",
            observed_at=datetime.now(UTC),
            context=foreign,
        )
    )
    telemetry.publish(
        TelemetrySignal.create(
            kind=TelemetryKind.METRIC,
            name="requests",
            observed_at=datetime.now(UTC),
            attributes={"value": "unknown"},
        )
    )
    assert metrics.cards(actor)[0].total == 1
    now[0] += 60
    assert metrics.record("requests", 2, {})
    assert metrics.cards(actor)[0].total == 3
    changed = MetricsService(
        SQLiteMetricsStore(path),
        (MetricDefinition("requests", MetricKind.GAUGE),),
        tenant_id="a",
        clock=lambda: now[0],
    )
    assert not changed.record("requests", 10, {})
    assert len(restarted.entries(actor)) == 2
    path.unlink()
    assert not inspector.collect(
        InspectorKind.LOG, "request.completed", event_id="no-store", fields={}
    )
    assert not metrics.record("requests", 1, {})
    with pytest.raises(ObservabilityUnavailable):
        inspector.entries(actor)
    with pytest.raises(ObservabilityUnavailable):
        metrics.cards(actor)
