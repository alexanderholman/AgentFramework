from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.application.program_jobs import ProgramJobHandler, ProgramJobService
from af.framework.context import CancellationToken
from af.framework.jobs import JobState
from af.framework.programs import ProgramBudget, ProgramRequest, ProgramState
from af.infrastructure.database import Database
from af.infrastructure.job_events import SqlAlchemyJobEventRepository
from af.infrastructure.program_job_results import SqlAlchemyProgramJobResultRepository
from af.infrastructure.programs import AsyncSubprocessExecutor
from af.messaging.envelopes import MessageEnvelope
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
        argv=(
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "Path('job-runs.txt').open('a').write('x'); print('job-output')",
        ),
        working_directory=tmp_path,
        budget=ProgramBudget(timeout=timedelta(seconds=3), max_output_bytes=256),
    )

    job_id = service.submit(request, correlation_id="phone-request-1")
    assert queue.jobs[0].timeout is None
    assert queue.jobs[0].envelope.metadata.correlation_id == "phone-request-1"
    handler = ProgramJobHandler(AsyncSubprocessExecutor(), events, results)
    handler.invoke(queue.jobs[0], cancellation=CancellationToken())
    handler.invoke(queue.jobs[0], cancellation=CancellationToken())

    assert (tmp_path / "job-runs.txt").read_text() == "x"
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


def test_program_job_rejects_malformed_timeout_payload(tmp_path: Path) -> None:
    database, events, results = _dependencies(tmp_path)
    queue = RecordingQueue()
    service = ProgramJobService(queue, events, results)
    job_id = service.submit(
        ProgramRequest(
            argv=(sys.executable, "-c", "pass"),
            working_directory=tmp_path,
            budget=ProgramBudget(timeout=timedelta(seconds=3), max_output_bytes=256),
        )
    )
    original_job = queue.jobs[0]
    envelope = original_job.envelope
    payload = envelope.payload_copy()
    payload["timeout_seconds"] = "3"
    malformed_envelope = MessageEnvelope.create(metadata=envelope.metadata, payload=payload)
    malformed_job = QueueJob(
        envelope=malformed_envelope,
        lane=original_job.lane,
        max_attempts=1,
        timeout=original_job.timeout,
    )

    with pytest.raises(ValueError, match="payload is invalid"):
        ProgramJobHandler(AsyncSubprocessExecutor(), events, results).invoke(
            malformed_job, cancellation=CancellationToken()
        )

    assert [event.state for event in service.events_after(job_id)] == [JobState.QUEUED]
    database.close()


def test_timed_out_program_job_reports_budget_exhausted(tmp_path: Path) -> None:
    database, events, results = _dependencies(tmp_path)
    queue = RecordingQueue()
    service = ProgramJobService(queue, events, results)
    job_id = service.submit(
        ProgramRequest(
            argv=(sys.executable, "-c", "import time; time.sleep(30)"),
            working_directory=tmp_path,
            budget=ProgramBudget(timeout=timedelta(milliseconds=50), max_output_bytes=256),
        )
    )

    ProgramJobHandler(AsyncSubprocessExecutor(), events, results).invoke(
        queue.jobs[0], cancellation=CancellationToken()
    )

    assert [event.state for event in service.events_after(job_id)][-1] is JobState.BUDGET_EXHAUSTED
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
