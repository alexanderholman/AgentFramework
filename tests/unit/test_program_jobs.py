from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from af.application.program_jobs import ProgramJobHandler, ProgramJobService
from af.framework.context import CancellationToken
from af.framework.jobs import JobState
from af.framework.programs import ProgramBudget, ProgramRequest, ProgramState
from af.infrastructure.database import Database
from af.infrastructure.job_events import SqlAlchemyJobEventRepository
from af.infrastructure.program_job_results import SqlAlchemyProgramJobResultRepository
from af.infrastructure.programs import AsyncSubprocessExecutor
from af.queue.contracts import QueueDispatchDisposition, QueueDispatchReceipt, QueueJob


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[QueueJob] = []

    def dispatch(self, job: QueueJob, *, name: str | None = None) -> QueueDispatchReceipt:
        self.jobs.append(job)
        return QueueDispatchReceipt(
            message_id=job.envelope.metadata.message_id,
            lane=job.lane,
            disposition=QueueDispatchDisposition.ENQUEUED,
            dispatched_at=datetime.now(UTC),
        )


def _dependencies(tmp_path: Path):
    database = Database(f"sqlite:///{tmp_path / 'program-jobs.sqlite3'}")
    events = SqlAlchemyJobEventRepository(database)
    results = SqlAlchemyProgramJobResultRepository(database)
    return database, events, results


def test_program_job_runs_from_queue_and_persists_result(tmp_path: Path) -> None:
    database, events, results = _dependencies(tmp_path)
    queue = RecordingQueue()
    service = ProgramJobService(queue, events, results)
    request = ProgramRequest(
        argv=(sys.executable, "-c", "print('job-output')"),
        working_directory=tmp_path,
        budget=ProgramBudget(timeout=timedelta(seconds=3), max_output_bytes=256),
    )

    job_id = service.submit(request, correlation_id="phone-request-1")
    assert queue.jobs[0].envelope.metadata.correlation_id == "phone-request-1"
    ProgramJobHandler(AsyncSubprocessExecutor(), events, results).invoke(
        queue.jobs[0], cancellation=CancellationToken()
    )

    saved = service.result(job_id)
    assert saved is not None
    assert saved.result.state is ProgramState.COMPLETED
    assert saved.result.stdout == b"job-output\n"
    assert [event.state for event in service.events_after(job_id)] == [
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.COMPLETED,
    ]
    database.close()


def test_cancelling_program_job_stops_process_and_persists_state(tmp_path: Path) -> None:
    database, events, results = _dependencies(tmp_path)
    queue = RecordingQueue()
    service = ProgramJobService(queue, events, results)
    job_id = service.submit(
        ProgramRequest(
            argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            working_directory=tmp_path,
            budget=ProgramBudget(timeout=timedelta(seconds=60), max_output_bytes=256),
        )
    )
    cancellation = CancellationToken()
    handler = ProgramJobHandler(AsyncSubprocessExecutor(), events, results)
    thread = threading.Thread(
        target=handler.invoke,
        args=(queue.jobs[0],),
        kwargs={"cancellation": cancellation},
    )
    thread.start()
    time.sleep(0.1)
    cancellation.cancel()
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert [event.state for event in service.events_after(job_id)][-1] is JobState.CANCELLED
    assert results.get(job_id) is None
    database.close()
