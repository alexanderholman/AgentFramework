"""Bounded, redacted operations projection shared by every operator interface."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from af.domain.tasks import Task, TaskRepository
from af.framework.application import ApplicationState

OPERATIONS_ACTIVITY_WINDOW = 25
OPERATIONS_RECENT_TASK_LIMIT = 10

type LifecycleStateValue = Literal[
    "constructed",
    "registered",
    "booting",
    "booted",
    "serving",
    "draining",
    "shutting_down",
    "shutdown",
    "failed",
]
type LivenessValue = Literal["live", "not_live"]
type ReadinessValue = Literal["ready", "not_ready", "unknown"]


class OperationsTaskReader(Protocol):
    @property
    def available(self) -> bool: ...

    def list_recent(self, *, limit: int) -> list[Task]: ...


class RepositoryOperationsTaskReader:
    """Adapt repositories predating the optional recent-Task capability safely."""

    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository
        candidate = getattr(repository, "list_recent", None)
        self._list_recent = cast(
            Callable[..., list[Task]] | None,
            candidate if callable(candidate) else None,
        )

    @property
    def available(self) -> bool:
        return self._list_recent is not None

    def list_recent(self, *, limit: int) -> list[Task]:
        if self._list_recent is None:
            return []
        return self._list_recent(limit=limit)[:limit]


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    lifecycle_state: LifecycleStateValue
    liveness: LivenessValue
    readiness: ReadinessValue
    reason: str


@dataclass(frozen=True, slots=True)
class TaskActivitySnapshot:
    available: bool
    window_limit: int
    observed: int
    by_status: tuple[tuple[str, int], ...]
    verified: int
    reused: int


@dataclass(frozen=True, slots=True)
class RecentTaskSnapshot:
    id: str
    status: str
    route: str
    tier: str
    verified: bool
    reused: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class OperationsSnapshot:
    schema_version: Literal["1"]
    observed_at: datetime
    runtime: RuntimeSnapshot
    activity: TaskActivitySnapshot
    recent_tasks: tuple[RecentTaskSnapshot, ...]


def _runtime_snapshot(state: ApplicationState) -> RuntimeSnapshot:
    if state is ApplicationState.SERVING:
        return RuntimeSnapshot(
            lifecycle_state=cast(LifecycleStateValue, state.value),
            liveness="live",
            readiness="unknown",
            reason="component readiness has not been observed",
        )
    if state is ApplicationState.DRAINING:
        return RuntimeSnapshot(
            lifecycle_state=cast(LifecycleStateValue, state.value),
            liveness="live",
            readiness="not_ready",
            reason="application is draining",
        )
    reason = (
        "application lifecycle failed"
        if state is ApplicationState.FAILED
        else f"application is {state.value}"
    )
    return RuntimeSnapshot(
        lifecycle_state=cast(LifecycleStateValue, state.value),
        liveness="not_live",
        readiness="not_ready",
        reason=reason,
    )


class OperationsService:
    def __init__(
        self,
        tasks: OperationsTaskReader,
        *,
        lifecycle_state: Callable[[], ApplicationState],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._tasks = tasks
        self._lifecycle_state = lifecycle_state
        self._clock = clock or (lambda: datetime.now(UTC))

    def snapshot(self) -> OperationsSnapshot:
        tasks = self._tasks.list_recent(limit=OPERATIONS_ACTIVITY_WINDOW)[
            :OPERATIONS_ACTIVITY_WINDOW
        ]
        status_counts = Counter(task.status for task in tasks)
        recent_tasks = tuple(
            RecentTaskSnapshot(
                id=task.id,
                status=task.status,
                route=task.route,
                tier=task.tier,
                verified=task.verified,
                reused=task.reused,
                created_at=task.created_at,
                updated_at=task.updated_at,
            )
            for task in tasks[:OPERATIONS_RECENT_TASK_LIMIT]
        )
        return OperationsSnapshot(
            schema_version="1",
            observed_at=self._clock(),
            runtime=_runtime_snapshot(self._lifecycle_state()),
            activity=TaskActivitySnapshot(
                available=self._tasks.available,
                window_limit=OPERATIONS_ACTIVITY_WINDOW,
                observed=len(tasks),
                by_status=tuple(sorted(status_counts.items())),
                verified=sum(task.verified for task in tasks),
                reused=sum(task.reused for task in tasks),
            ),
            recent_tasks=recent_tasks,
        )


class OperationsController:
    def __init__(self, service: OperationsService) -> None:
        self._service = service

    def snapshot(self) -> OperationsSnapshot:
        return self._service.snapshot()
