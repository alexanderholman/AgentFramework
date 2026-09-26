"""Persisted Task model and repository port."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol, runtime_checkable

from af.framework.errors import AFError


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    prompt: str
    output: str
    intent: str
    route: str
    tier: str
    status: str
    verified: bool
    reused: bool
    trace_id: str
    created_at: datetime
    updated_at: datetime


class TaskRepository(Protocol):
    def save(self, task: Task) -> Task: ...

    def get(self, task_id: str) -> Task: ...

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[Task]: ...

    def delete(self, task_id: str) -> None: ...


class TaskConflict(AFError):
    """The persisted Task no longer matches the caller's observed version."""

    def __init__(self) -> None:
        super().__init__("task precondition failed")


def task_version(task: Task) -> str:
    """Stable resource version across process restarts and representations."""
    payload = asdict(task)
    payload["created_at"] = task.created_at.isoformat()
    payload["updated_at"] = task.updated_at.isoformat()
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@runtime_checkable
class ConditionalTaskRepository(Protocol):
    def compare_and_save(self, task: Task, *, expected: Task) -> Task: ...

    def compare_and_delete(self, expected: Task) -> None: ...
