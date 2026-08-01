from __future__ import annotations

from datetime import UTC, datetime

from af.application.search import SearchService
from af.domain.tasks import Task
from af.framework.types import RunTrace, TraceEvent
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository


def _task(task_id: str, created_at: datetime) -> Task:
    return Task(
        id=task_id,
        prompt=f"prompt {task_id}",
        output=f"output {task_id}",
        intent="text.character_count",
        route="character-count",
        tier="procedural",
        status="completed",
        verified=True,
        reused=False,
        trace_id=f"trace-{task_id}",
        created_at=created_at,
        updated_at=created_at,
    )


def _trace(trace_id: str, created_at: datetime) -> RunTrace:
    return RunTrace(
        id=trace_id,
        prompt=f"trace prompt {trace_id}",
        intent="text.character_count",
        arguments={"text": trace_id, "character": "t"},
        route="character-count",
        tier="procedural",
        output="1",
        verified=True,
        reused=False,
        regression_passed=True,
        promoted=False,
        status="completed",
        events=(TraceEvent(stage="execute", detail=trace_id),),
        created_at=created_at,
    )


class RecordingSearchManager:
    def __init__(self) -> None:
        self.batches: list[tuple[object, ...]] = []

    def upsert(self, resources: tuple[object, ...], *, name: str | None = None) -> int:
        del name
        self.batches.append(resources)
        return len(resources)


def test_search_index_limit_bounds_total_resources_across_all_sources() -> None:
    tasks = InMemoryTaskRepository()
    traces = InMemoryTraceRepository()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    tasks.save(_task("task-1", now))
    tasks.save(_task("task-2", now.replace(second=1)))
    traces.save(_trace("trace-1", now.replace(second=2)))
    traces.save(_trace("trace-2", now.replace(second=3)))
    manager = RecordingSearchManager()
    service = SearchService(manager, tasks, traces)  # type: ignore[arg-type]

    result = service.index(limit=3)

    assert result.indexed == 3
    assert result.sources == (
        result.sources[0].__class__(source="tasks", indexed=2),
        result.sources[1].__class__(source="traces", indexed=1),
    )
    assert len(manager.batches) == 2
    assert len(manager.batches[0]) == 2
    assert len(manager.batches[1]) == 1
    assert sum(len(batch) for batch in manager.batches) == 3
