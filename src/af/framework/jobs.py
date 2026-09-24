"""Durable job status and progress event contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from af.framework.programs import ProgramResult

__all__ = (
    "JobEvent",
    "JobEventRepository",
    "JobState",
    "ProgramJobResult",
    "ProgramJobResultRepository",
)

_IDENTIFIER = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_STAGE = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,62}[a-z0-9])?$")


class JobState(StrEnum):
    QUEUED = "queued"
    WAITING_FOR_NODE = "waiting_for_node"
    LOADING_MODEL = "loading_model"
    RUNNING = "running"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class JobEvent:
    job_id: str
    sequence: int
    state: JobState
    created_at: datetime
    stage: str | None = None
    progress: float | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if type(self.job_id) is not str or _IDENTIFIER.fullmatch(self.job_id) is None:
            raise ValueError("job_id must be a UUID")
        if type(self.sequence) is not int or self.sequence < 1:
            raise ValueError("sequence must be positive")
        if not isinstance(self.state, JobState):
            raise ValueError("state must be a JobState")
        if not isinstance(self.created_at, datetime) or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        if self.stage is not None and (
            type(self.stage) is not str or _STAGE.fullmatch(self.stage) is None
        ):
            raise ValueError("stage must be a canonical identifier")
        if self.progress is not None and (
            type(self.progress) not in {int, float} or not 0.0 <= self.progress <= 1.0
        ):
            raise ValueError("progress must be between zero and one")
        if self.detail is not None and (
            type(self.detail) is not str or len(self.detail) > 256 or "\x00" in self.detail
        ):
            raise ValueError("detail must be bounded text")


class JobEventRepository(Protocol):
    def append(
        self,
        job_id: str,
        state: JobState,
        *,
        stage: str | None = None,
        progress: float | None = None,
        detail: str | None = None,
    ) -> JobEvent: ...

    def list_after(self, job_id: str, *, sequence: int = 0, limit: int = 100) -> list[JobEvent]: ...


@dataclass(frozen=True, slots=True)
class ProgramJobResult:
    job_id: str
    result: ProgramResult
    finished_at: datetime

    def __post_init__(self) -> None:
        if type(self.job_id) is not str or _IDENTIFIER.fullmatch(self.job_id) is None:
            raise ValueError("job_id must be a UUID")
        if not isinstance(self.result, ProgramResult):
            raise ValueError("result must be a ProgramResult")
        if not isinstance(self.finished_at, datetime) or self.finished_at.utcoffset() is None:
            raise ValueError("finished_at must be timezone-aware")
        object.__setattr__(self, "finished_at", self.finished_at.astimezone(UTC))


class ProgramJobResultRepository(Protocol):
    def claim(self, job_id: str) -> bool: ...

    def save(self, value: ProgramJobResult) -> None: ...

    def get(self, job_id: str) -> ProgramJobResult | None: ...
