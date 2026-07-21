"""Persisted Task model and repository port."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


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
