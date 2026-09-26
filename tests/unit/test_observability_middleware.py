"""Observers run after persistence and cannot replace dispatch results or cancellation."""

import asyncio
from pathlib import Path

import pytest

from af.application.bus import ApplicationResult, DispatchRequest, MessageKind
from af.application.dispatch import CreateTask, ExecuteTask, GetTask
from af.application.observability_middleware import ObservationMiddleware
from af.application.observability_products import InspectorService, MetricsService
from af.bootstrap import create_application
from af.framework.context import ExecutionContext, Principal
from af.framework.observability_products import InspectorPolicy, MetricDefinition, MetricKind
from af.infrastructure.observability_products import SQLiteInspectorStore, SQLiteMetricsStore
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository


def test_observer_runs_after_persistence_and_sink_failure_does_not_change_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inspector = InspectorService(
        SQLiteInspectorStore(tmp_path / "inspect"),
        InspectorPolicy(("af.task.completed",)),
        tenant_id="a",
    )
    application = create_application(
        task_repository=InMemoryTaskRepository(), trace_repository=InMemoryTraceRepository()
    )

    def failure(*args: object, **kwargs: object) -> bool:
        assert len(application.tasks.list()) == 1
        raise OSError("optional inspector failed")

    monkeypatch.setattr(inspector, "collect", failure)
    middleware = ObservationMiddleware(inspector=inspector)
    request = DispatchRequest(CreateTask("hello"), MessageKind.COMMAND, ExecutionContext())

    async def persist(request: DispatchRequest) -> ApplicationResult[object]:
        return ApplicationResult(application.tasks.create("hello"))

    result = asyncio.run(middleware(request, persist))
    assert result.value == application.tasks.list()[0]


def test_observer_retains_error_and_cancellation_and_ignores_unrelated_queries(
    tmp_path: Path,
) -> None:
    metrics = MetricsService(
        SQLiteMetricsStore(tmp_path / "metrics"),
        (
            MetricDefinition(
                "af.task.completed",
                MetricKind.COUNTER,
                (("operation", ("execute",)), ("outcome", ("ok", "error", "cancelled"))),
            ),
            MetricDefinition(
                "af.task.duration",
                MetricKind.HISTOGRAM,
                (("operation", ("execute",)), ("outcome", ("ok", "error", "cancelled"))),
                (1.0, 10.0),
            ),
        ),
        tenant_id="a",
    )
    middleware = ObservationMiddleware(metrics=metrics)
    request = DispatchRequest(ExecuteTask("hello"), MessageKind.COMMAND, ExecutionContext())

    async def failed(request: DispatchRequest) -> ApplicationResult[object]:
        raise ValueError("original failure")

    with pytest.raises(ValueError, match="original failure"):
        asyncio.run(middleware(request, failed))

    async def cancelled(request: DispatchRequest) -> ApplicationResult[object]:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(middleware(request, cancelled))

    async def succeeds(request: DispatchRequest) -> ApplicationResult[object]:
        return ApplicationResult("same")

    assert (
        asyncio.run(
            middleware(
                DispatchRequest(GetTask("id"), MessageKind.QUERY, ExecutionContext()), succeeds
            )
        ).value
        == "same"
    )
    cards = metrics.cards(Principal("op", "user", scopes=("af:observability:read",)))
    assert {dict(card.labels)["outcome"] for card in cards} == {"error", "cancelled"}
