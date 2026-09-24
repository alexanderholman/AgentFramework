from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from af.framework.programs import ProgramBudget, ProgramRequest, ProgramState
from af.infrastructure.programs import AsyncSubprocessExecutor


@pytest.mark.asyncio
async def test_executes_program_and_captures_stdout_and_stderr(tmp_path: Path) -> None:
    executor = AsyncSubprocessExecutor()
    request = ProgramRequest(
        argv=(sys.executable, "-c", "import sys; print('ready'); print('note', file=sys.stderr)"),
        working_directory=tmp_path,
        budget=ProgramBudget(timeout=timedelta(seconds=3), max_output_bytes=1_024),
    )

    result = await executor.execute(request)

    assert result.state is ProgramState.COMPLETED
    assert result.exit_code == 0
    assert result.stdout == b"ready\n"
    assert result.stderr == b"note\n"
    assert result.elapsed >= timedelta(0)


@pytest.mark.asyncio
async def test_nonzero_exit_is_a_completed_failure_with_captured_output(tmp_path: Path) -> None:
    executor = AsyncSubprocessExecutor()
    request = ProgramRequest(
        argv=(sys.executable, "-c", "import sys; print('bad'); sys.exit(7)"),
        working_directory=tmp_path,
        budget=ProgramBudget(timeout=timedelta(seconds=3), max_output_bytes=1_024),
    )

    result = await executor.execute(request)

    assert result.state is ProgramState.FAILED
    assert result.exit_code == 7
    assert result.stdout == b"bad\n"


@pytest.mark.asyncio
async def test_timeout_terminates_the_program_and_returns_partial_output(tmp_path: Path) -> None:
    executor = AsyncSubprocessExecutor()
    request = ProgramRequest(
        argv=(sys.executable, "-c", "import time; print('starting', flush=True); time.sleep(30)"),
        working_directory=tmp_path,
        budget=ProgramBudget(timeout=timedelta(milliseconds=100), max_output_bytes=1_024),
    )

    result = await executor.execute(request)

    assert result.state is ProgramState.TIMED_OUT
    assert result.exit_code is None
    assert result.stdout == b"starting\n"
    assert result.elapsed < timedelta(seconds=3)


@pytest.mark.asyncio
async def test_output_budget_is_shared_between_stdout_and_stderr(tmp_path: Path) -> None:
    executor = AsyncSubprocessExecutor()
    request = ProgramRequest(
        argv=(
            sys.executable,
            "-c",
            "import sys; sys.stdout.write('a'*10000); sys.stderr.write('b'*10000)",
        ),
        working_directory=tmp_path,
        budget=ProgramBudget(timeout=timedelta(seconds=3), max_output_bytes=64),
    )

    result = await executor.execute(request)

    assert result.state is ProgramState.OUTPUT_LIMIT
    assert len(result.stdout) + len(result.stderr) <= 64


@pytest.mark.asyncio
async def test_caller_cancellation_stops_the_program(tmp_path: Path) -> None:
    executor = AsyncSubprocessExecutor()
    request = ProgramRequest(
        argv=(sys.executable, "-c", "import time; time.sleep(30)"),
        working_directory=tmp_path,
        budget=ProgramBudget(timeout=timedelta(seconds=30), max_output_bytes=1_024),
    )
    task = asyncio.create_task(executor.execute(request))
    await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_program_budget_accepts_ten_minute_startup_deadline() -> None:
    budget = ProgramBudget(timeout=timedelta(seconds=600), max_output_bytes=1_024)

    assert budget.timeout == timedelta(minutes=10)


@pytest.mark.parametrize(
    ("argv", "working_directory"),
    [
        ((), "/tmp"),
        (("python", "\x00"), "/tmp"),
        (("python",), ""),
    ],
)
def test_program_request_rejects_invalid_shapes(argv: object, working_directory: object) -> None:
    with pytest.raises(ValueError):
        ProgramRequest(
            argv=argv,  # type: ignore[arg-type]
            working_directory=working_directory,  # type: ignore[arg-type]
            budget=ProgramBudget(timeout=timedelta(seconds=1), max_output_bytes=100),
        )
