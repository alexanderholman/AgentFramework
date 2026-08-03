from __future__ import annotations

from datetime import UTC, datetime, timedelta

from af.application.operations import OperationsService
from af.domain.tasks import Task
from af.framework.application import ApplicationState


class _RecentTaskRepository:
    def __init__(self, tasks: list[Task]) -> None:
        self.tasks = tasks
        self.requested_limits: list[int] = []

    def list_recent(self, *, limit: int) -> list[Task]:
        self.requested_limits.append(limit)
        return self.tasks[:limit]


def _task(index: int, *, status: str, verified: bool, reused: bool) -> Task:
    observed_at = datetime(2026, 8, 3, 12, index, tzinfo=UTC)
    return Task(
        id=f"task-{index}",
        prompt=f"secret prompt {index}",
        output=f"secret output {index}",
        intent="system.hello",
        route="hello",
        tier="static",
        status=status,
        verified=verified,
        reused=reused,
        trace_id=f"secret-trace-{index}",
        created_at=observed_at,
        updated_at=observed_at + timedelta(seconds=1),
    )


def test_operations_snapshot_is_bounded_redacted_and_deterministic() -> None:
    repository = _RecentTaskRepository(
        [
            _task(3, status="completed", verified=True, reused=True),
            _task(2, status="completed", verified=True, reused=False),
            _task(1, status="unsupported", verified=False, reused=False),
        ]
    )
    observed_at = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)
    service = OperationsService(
        repository,
        lifecycle_state=lambda: ApplicationState.SERVING,
        clock=lambda: observed_at,
    )

    snapshot = service.snapshot()

    assert repository.requested_limits == [25]
    assert snapshot.schema_version == "1"
    assert snapshot.observed_at == observed_at
    assert snapshot.runtime.lifecycle_state == "serving"
    assert snapshot.runtime.liveness == "live"
    assert snapshot.runtime.readiness == "unknown"
    assert snapshot.activity.window_limit == 25
    assert snapshot.activity.observed == 3
    assert snapshot.activity.by_status == (("completed", 2), ("unsupported", 1))
    assert snapshot.activity.verified == 2
    assert snapshot.activity.reused == 1
    assert [task.id for task in snapshot.recent_tasks] == ["task-3", "task-2", "task-1"]
    rendered = repr(snapshot)
    assert "secret prompt" not in rendered
    assert "secret output" not in rendered
    assert "secret-trace" not in rendered


def test_operations_snapshot_reports_non_serving_states_without_false_readiness() -> None:
    repository = _RecentTaskRepository([])
    service = OperationsService(
        repository,
        lifecycle_state=lambda: ApplicationState.DRAINING,
        clock=lambda: datetime(2026, 8, 3, tzinfo=UTC),
    )

    snapshot = service.snapshot()

    assert snapshot.runtime.lifecycle_state == "draining"
    assert snapshot.runtime.liveness == "live"
    assert snapshot.runtime.readiness == "not_ready"
    assert "draining" in snapshot.runtime.reason
