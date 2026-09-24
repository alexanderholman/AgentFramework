"""Contracts for bounded programs executed by an agent host."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

__all__ = (
    "ProgramBudget",
    "ProgramExecutor",
    "ProgramRequest",
    "ProgramResult",
    "ProgramState",
)


class ProgramState(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT = "output_limit"


@dataclass(frozen=True, slots=True)
class ProgramBudget:
    timeout: timedelta
    max_output_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.timeout, timedelta) or self.timeout <= timedelta(0):
            raise ValueError("timeout must be positive")
        if type(self.max_output_bytes) is not int or self.max_output_bytes < 1:
            raise ValueError("max_output_bytes must be a positive integer")


@dataclass(frozen=True, slots=True)
class ProgramRequest:
    argv: tuple[str, ...]
    working_directory: Path
    budget: ProgramBudget
    environment: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple) or not self.argv:
            raise ValueError("argv must be a non-empty tuple")
        if any(
            type(argument) is not str or not argument or "\x00" in argument
            for argument in self.argv
        ):
            raise ValueError("argv entries must be non-empty strings without NUL")
        if not isinstance(self.working_directory, Path) or not str(self.working_directory):
            raise ValueError("working_directory must be a non-empty Path")
        if not isinstance(self.budget, ProgramBudget):
            raise ValueError("budget must be a ProgramBudget")
        if self.environment is not None:
            if not isinstance(self.environment, Mapping):
                raise ValueError("environment must be a mapping")
            if any(
                type(key) is not str
                or not key
                or "=" in key
                or "\x00" in key
                or type(value) is not str
                or "\x00" in value
                for key, value in self.environment.items()
            ):
                raise ValueError("environment entries must be valid strings")


@dataclass(frozen=True, slots=True)
class ProgramResult:
    state: ProgramState
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    elapsed: timedelta


class ProgramExecutor(Protocol):
    async def execute(self, request: ProgramRequest) -> ProgramResult: ...
