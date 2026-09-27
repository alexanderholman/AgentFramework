"""Removable local inspector and metric products with finite installed vocabularies."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from af.application.admin import create_reference_admin
from af.application.observability_products import InspectorService, MetricsService
from af.application.operations import OperationsController
from af.application.tasks import TaskController
from af.config import Settings
from af.framework.observability_products import InspectorPolicy, MetricDefinition, MetricKind
from af.infrastructure.database import Database
from af.infrastructure.observability_products import SQLiteInspectorStore, SQLiteMetricsStore
from af.interfaces.observability_products import ObservabilityProducts


def create_observability_products(settings: Settings, database: Database) -> ObservabilityProducts:
    if not settings.inspector_enabled and not settings.metrics_enabled:
        return ObservabilityProducts()
    url = database.engine.url
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        raise ValueError("reference observational products require persistent SQLite")
    path = Path(str(url.database)).expanduser().resolve()
    labels = (
        ("operation", ("execute", "create", "update", "count")),
        ("outcome", ("ok", "error", "cancelled")),
    )
    inspector = (
        InspectorService(
            SQLiteInspectorStore(path),
            InspectorPolicy(names=("af.task.completed", "af.request.completed")),
            tenant_id=settings.auth_tenant_id,
        )
        if settings.inspector_enabled
        else None
    )
    metrics = (
        MetricsService(
            SQLiteMetricsStore(path),
            (
                MetricDefinition("af.task.completed", MetricKind.COUNTER, labels),
                MetricDefinition(
                    "af.task.duration",
                    MetricKind.HISTOGRAM,
                    labels,
                    bounds=(1.0, 10.0, 100.0, 1000.0),
                ),
            ),
            tenant_id=settings.auth_tenant_id,
        )
        if settings.metrics_enabled
        else None
    )
    return ObservabilityProducts(inspector=inspector, metrics=metrics)


def with_reference_admin(
    products: ObservabilityProducts, tasks: TaskController, operations: OperationsController
) -> ObservabilityProducts:
    """Attach application-owned panels only after shared controllers are constructed."""
    return replace(products, admin=create_reference_admin(tasks, operations))
