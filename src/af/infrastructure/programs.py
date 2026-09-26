"""POSIX subprocess implementation of the agent host program executor."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from contextlib import suppress
from datetime import timedelta

from af.framework.programs import ProgramRequest, ProgramResult, ProgramState

__all__ = ("AsyncSubprocessExecutor",)


class AsyncSubprocessExecutor:
    """Run argv directly, with bounded capture and process-tree cleanup."""

    async def execute(self, request: ProgramRequest) -> ProgramResult:
        started = time.monotonic()
        environment = None
        if request.environment is not None:
            environment = os.environ.copy()
            environment.update(request.environment)
        process = await asyncio.create_subprocess_exec(
            *request.argv,
            cwd=request.working_directory,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=max(65_536, request.budget.max_output_bytes + 1),
        )
        assert process.stdout is not None
        assert process.stderr is not None
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        remaining = request.budget.max_output_bytes
        exceeded = asyncio.Event()

        async def drain(stream: asyncio.StreamReader, name: str) -> None:
            nonlocal remaining
            while chunk := await stream.read(16_384):
                kept = chunk[:remaining]
                buffers[name].extend(kept)
                remaining -= len(kept)
                if len(kept) < len(chunk):
                    exceeded.set()
                    return

        drains = [
            asyncio.create_task(drain(process.stdout, "stdout")),
            asyncio.create_task(drain(process.stderr, "stderr")),
        ]

        async def wait_for_capture() -> None:
            await process.wait()
            await asyncio.gather(*drains)

        wait_process = asyncio.create_task(wait_for_capture())
        wait_limit = asyncio.create_task(exceeded.wait())
        state = ProgramState.COMPLETED
        try:
            done, _ = await asyncio.wait(
                {wait_process, wait_limit},
                timeout=request.budget.timeout.total_seconds(),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if exceeded.is_set():
                state = ProgramState.OUTPUT_LIMIT
                await self._terminate(process)
            elif wait_process not in done:
                state = ProgramState.TIMED_OUT
                await self._terminate(process)
            await asyncio.gather(*drains)
            exit_code = process.returncode
            if state is ProgramState.COMPLETED and exit_code != 0:
                state = ProgramState.FAILED
            return ProgramResult(
                state=state,
                exit_code=(
                    exit_code if state in {ProgramState.COMPLETED, ProgramState.FAILED} else None
                ),
                stdout=bytes(buffers["stdout"]),
                stderr=bytes(buffers["stderr"]),
                elapsed=timedelta(seconds=time.monotonic() - started),
            )
        except asyncio.CancelledError:
            await self._terminate(process)
            for task in drains:
                task.cancel()
            await asyncio.gather(*drains, return_exceptions=True)
            raise
        finally:
            wait_limit.cancel()
            wait_process.cancel()
            await asyncio.gather(wait_limit, wait_process, return_exceptions=True)

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        # Descendants may keep capture pipes alive after the leader exits.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        await asyncio.sleep(0.25)
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
