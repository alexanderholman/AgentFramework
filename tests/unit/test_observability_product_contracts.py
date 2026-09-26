"""Finite registries and installed admin callbacks reject authority and size widening."""

from pathlib import Path

import pytest

from af.application.admin import AdminService
from af.application.observability_products import InspectorService, MetricsService
from af.framework.admin import AdminAction, AdminActionResult, AdminResource, AdminRow
from af.framework.context import Principal
from af.framework.observability_products import (
    InspectorPolicy,
    MetricDefinition,
    MetricKind,
    ObservabilityDenied,
)
from af.infrastructure.observability_products import SQLiteInspectorStore, SQLiteMetricsStore


def test_observation_registries_enforce_all_finite_domains(tmp_path: Path) -> None:
    for parameters in (
        {"names": ()},
        {"names": ("bad name",)},
        {"names": ("event",), "tags": ("x", "x")},
        {"names": ("event",), "sample_rate": float("nan")},
        {"names": ("event",), "max_records": 0},
    ):
        with pytest.raises(ValueError):
            InspectorPolicy(**parameters)
    for parameters in (
        {"kind": "counter"},
        {"label_domains": (("status", ()),)},
        {
            "label_domains": (
                ("a", tuple("abcdefghijklmnop")),
                ("b", tuple("abcdefghijklmnop")),
                ("c", ("one", "two")),
            )
        },
        {"bounds": (2.0, 1.0)},
        {"kind": MetricKind.HISTOGRAM},
    ):
        with pytest.raises(ValueError):
            MetricDefinition(**({"name": "metric", "kind": MetricKind.COUNTER} | parameters))
    for store in (SQLiteInspectorStore, SQLiteMetricsStore):
        with pytest.raises(ValueError):
            store(":memory:")
    with pytest.raises(ValueError):
        InspectorService(
            SQLiteInspectorStore(tmp_path / "a"), InspectorPolicy(("event",)), tenant_id=""
        )
    with pytest.raises(ValueError):
        MetricsService(SQLiteMetricsStore(tmp_path / "b"), (), tenant_id="a")
    with pytest.raises(ValueError):
        MetricsService(
            SQLiteMetricsStore(tmp_path / "b"),
            (MetricDefinition("metric", MetricKind.COUNTER),),
            tenant_id="a",
            max_rows=0,
        )
    metrics = MetricsService(
        SQLiteMetricsStore(tmp_path / "b"),
        (MetricDefinition("metric", MetricKind.COUNTER),),
        tenant_id="a",
    )
    assert not metrics.record("unknown", 1, {})
    assert not metrics.record("metric", float("inf"), {})


def test_admin_authorization_validation_and_callback_bounds() -> None:
    actor = Principal("op", "user", scopes=("resource:read", "resource:write"))
    guest = Principal("guest", "user")
    calls: list[object] = []

    def query(principal: Principal, limit: int) -> tuple[AdminRow, ...]:
        calls.append(principal)
        return (AdminRow("service", "ready", (("count", 1),)),)

    def execute(principal: Principal, parameters: object) -> AdminActionResult:
        calls.append(parameters)
        return AdminActionResult("completed", "service")

    action = AdminAction("restart", "resource:write", execute)
    resource = AdminResource("health", "resource:read", query, (action,))
    service = AdminService((resource,))
    assert service.resources(actor) == ("health",)
    assert service.resources(guest) == ()
    for operation in (
        lambda: service.query(guest, "health"),
        lambda: service.query(actor, "missing"),
        lambda: service.action(guest, "health", "restart", {}, confirmed=True),
        lambda: service.action(actor, "missing", "restart", {}, confirmed=True),
        lambda: service.action(actor, "health", "unknown", {}, confirmed=True),
    ):
        with pytest.raises(ObservabilityDenied):
            operation()
    assert calls == []
    with pytest.raises(ValueError):
        service.query(actor, "health", limit=101)
    with pytest.raises(ValueError):
        service.action(actor, "health", "restart", {"value": "x" * 201}, confirmed=True)
    with pytest.raises(ValueError):
        service.action(actor, "health", "restart", {str(i): "x" for i in range(17)}, confirmed=True)
    assert service.query(actor, "health")[0].reference == "service"
    assert service.action(actor, "health", "restart", {}, confirmed=True).status == "completed"
    for resources in (
        (resource, resource),
        (AdminResource("health", "", query),),
        (AdminResource("health", "read", query, (action, action)),),
        (AdminResource("health", "read", query, (AdminAction("restart", "", execute),)),),
    ):
        with pytest.raises(ValueError):
            AdminService(resources)
    for row in (
        lambda: AdminRow("", "ready"),
        lambda: AdminRow("x", "ready", (("password", "private"),)),
        lambda: AdminActionResult("invalid"),
    ):
        with pytest.raises(ValueError):
            row()
    invalid = AdminService((AdminResource("health", "resource:read", lambda principal, limit: ()),))
    assert invalid.query(actor, "health") == ()
