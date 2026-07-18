"""Task repository implementations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from af.domain.tasks import Task
from af.framework.errors import TaskNotFound
from af.infrastructure.database import Database, TaskRow


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def save(self, task: Task) -> Task:
        self._tasks[task.id] = task
        return task

    def get(self, task_id: str) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError as error:
            raise TaskNotFound(task_id) from error

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[Task]:
        tasks = sorted(self._tasks.values(), key=lambda task: task.created_at)
        if offset:
            tasks = tasks[offset:]
        if limit is not None:
            tasks = tasks[:limit]
        return tasks

    def delete(self, task_id: str) -> None:
        if task_id not in self._tasks:
            raise TaskNotFound(task_id)
        del self._tasks[task_id]


class SqlAlchemyTaskRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save(self, task: Task) -> Task:
        with self._database.sessions.begin() as session:
            row = session.get(TaskRow, task.id)
            if row is None:
                row = TaskRow(id=task.id)
                session.add(row)
            self._copy_to_row(task, row)
        return task

    def get(self, task_id: str) -> Task:
        with self._database.sessions() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFound(task_id)
            return self._to_domain(row)

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[Task]:
        with self._database.sessions() as session:
            statement = select(TaskRow).order_by(TaskRow.created_at).offset(offset)
            if limit is not None:
                statement = statement.limit(limit)
            rows = session.scalars(statement).all()
            return [self._to_domain(row) for row in rows]

    def delete(self, task_id: str) -> None:
        with self._database.sessions.begin() as session:
            row = session.get(TaskRow, task_id)
            if row is None:
                raise TaskNotFound(task_id)
            session.delete(row)

    @staticmethod
    def _copy_to_row(task: Task, row: TaskRow) -> None:
        row.prompt = task.prompt
        row.output = task.output
        row.intent = task.intent
        row.route = task.route
        row.tier = task.tier
        row.status = task.status
        row.verified = task.verified
        row.reused = task.reused
        row.trace_id = task.trace_id
        row.created_at = task.created_at
        row.updated_at = task.updated_at

    @staticmethod
    def _to_domain(row: TaskRow) -> Task:
        return Task(
            id=row.id,
            prompt=row.prompt,
            output=row.output,
            intent=row.intent,
            route=row.route,
            tier=row.tier,
            status=row.status,
            verified=row.verified,
            reused=row.reused,
            trace_id=row.trace_id,
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )
