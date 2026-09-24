"""Queue-backed host program jobs with durable progress and result retrieval."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import uuid4

from af.framework.context import CancellationToken
from af.framework.jobs import (
    JobEvent,
    JobEventRepository,
    JobState,
    ProgramJobResult,
    ProgramJobResultRepository,
)
from af.framework.programs import ProgramBudget, ProgramExecutor, ProgramRequest, ProgramState
from af.messaging.envelopes import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue.contracts import QueueDispatchReceipt, QueueJob

__all__ = ("ProgramJobHandler", "ProgramJobService")


class ProgramQueueDispatcher(Protocol):
    def dispatch(self, job: QueueJob, *, name: str | None = None) -> QueueDispatchReceipt: ...


class ProgramJobService:
    """Submit bounded host programs to AF's configured durable queue."""

    def __init__(self, queues: ProgramQueueDispatcher, events: JobEventRepository) -> None:
        self._queues = queues
        self._events = events

    def submit(self, request: ProgramRequest, *, correlation_id: str | None = None) -> str:
        if not isinstance(request, ProgramRequest):
            raise TypeError("program job requires a ProgramRequest")
        job_id = str(uuid4())
        now = datetime.now(UTC)
        envelope = MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=job_id,
                message_type="af.program.execute",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=now,
                correlation_id=correlation_id,
                deadline=now + request.budget.timeout,
            ),
            payload={
                "argv": list(request.argv),
                "working_directory": str(request.working_directory),
                "timeout_seconds": request.budget.timeout.total_seconds(),
                "max_output_bytes": request.budget.max_output_bytes,
                "environment": dict(request.environment) if request.environment is not None else None,
            },
        )
        self._events.append(job_id, JobState.QUEUED, stage="queue.dispatch")
        job = QueueJob(
            envelope=envelope,
            lane="agent",
            max_attempts=1,
            timeout=request.budget.timeout,
        )
        try:
            self._queues.dispatch(job)
        except Exception:
            self._events.append(job_id, JobState.FAILED, stage="queue.dispatch", detail="dispatch_failed")
            raise
        return job_id

    def events_after(self, job_id: str, *, sequence: int = 0, limit: int = 100) -> list[JobEvent]:
        return self._events.list_after(job_id, sequence=sequence, limit=limit)


class ProgramJobHandler:
    """Synchronous queue adapter for the async bounded subprocess executor."""

    cooperative_cancellation = True

    def __init__(
        self,
        executor: ProgramExecutor,
        events: JobEventRepository,
        results: ProgramJobResultRepository,
    ) -> None:
        self._executor = executor
        self._events = events
        self._results = results

    def __call__(self, job: QueueJob) -> None:
        self.invoke(job, cancellation=CancellationToken())

    def invoke(self, job: QueueJob, /, *, cancellation: CancellationToken) -> None:
        if not isinstance(job, QueueJob) or job.envelope.metadata.message_type != "af.program.execute":
            raise ValueError("unsupported program job")
        asyncio.run(self._execute(job, cancellation))

    async def _execute(self, job: QueueJob, cancellation: CancellationToken) -> None:
        job_id = job.envelope.metadata.message_id
        payload = job.envelope.payload_copy()
        if not isinstance(payload, Mapping):
            raise ValueError("program job payload is invalid")
        argv = payload.get("argv")
        cwd = payload.get("working_directory")
        timeout = payload.get("timeout_seconds")
        max_output = payload.get("max_output_bytes")
        environment = payload.get("environment")
        if (
            not isinstance(argv, list)
            or any(type(value) is not str for value in argv)
            or type(cwd) is not str
            or type(timeout) not in {int, float}
            or type(max_output) is not int
            or (environment is not None and not isinstance(environment, Mapping))
        ):
            raise ValueError("program job payload is invalid")
        request = ProgramRequest(
            argv=tuple(argv),
            working_directory=Path(cwd),
            budget=ProgramBudget(
                timeout=timedelta(seconds=timeout),
                max_output_bytes=max_output,
            ),
            environment=(
                cast(dict[str, str], dict(environment)) if environment is not None else None
            ),
        )
        self._events.append(job_id, JobState.RUNNING, stage="program.execute")
        run = asyncio.create_task(self._executor.execute(request))
        stop = asyncio.create_task(cancellation.wait())
        try:
            done, _ = await asyncio.wait({run, stop}, return_when=asyncio.FIRST_COMPLETED)
            if run not in done:
                run.cancel()
                await asyncio.gather(run, return_exceptions=True)
                self._events.append(job_id, JobState.CANCELLED, stage="program.execute")
                return
            result = run.result()
            completed = result.state is ProgramState.COMPLETED
            state = JobState.COMPLETED if completed else JobState.FAILED
            self._results.save(
                ProgramJobResult(job_id=job_id, result=result, finished_at=datetime.now(UTC))
            )
            self._events.append(
                job_id,
                state,
                stage="program.execute",
                progress=1.0,
                detail=result.state.value,
            )
        except Exception:
            self._events.append(
                job_id, JobState.FAILED, stage="program.execute", detail="execution_failed"
            )
            raise
        finally:
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)
