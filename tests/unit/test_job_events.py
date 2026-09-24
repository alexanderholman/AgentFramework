from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from af.framework.jobs import JobEvent, JobState
from af.infrastructure.database import Database
from af.infrastructure.job_events import SqlAlchemyJobEventRepository


def test_persisted_job_events_are_ordered_and_reconnectable(tmp_path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'jobs.sqlite3'}")
    repository = SqlAlchemyJobEventRepository(database)
    job_id = str(uuid4())

    first = repository.append(job_id, JobState.QUEUED)
    second = repository.append(
        job_id,
        JobState.LOADING_MODEL,
        stage="model.load",
        progress=0.25,
        detail="Waiting for local model startup",
    )
    repository.append(job_id, JobState.COMPLETED, progress=1.0)

    assert [event.sequence for event in repository.list_after(job_id)] == [1, 2, 3]
    assert repository.list_after(job_id, sequence=first.sequence) == [
        second,
        *repository.list_after(job_id, sequence=2),
    ]
    database.close()


@pytest.mark.parametrize(
    "values",
    [
        {"job_id": "bad"},
        {"sequence": 0},
        {"progress": 1.1},
        {"stage": "not a stage"},
        {"created_at": datetime.now()},
    ],
)
def test_job_event_rejects_invalid_contract(values: dict[str, object]) -> None:
    event_values: dict[str, object] = {
        "job_id": str(uuid4()),
        "sequence": 1,
        "state": JobState.RUNNING,
        "created_at": datetime.now(UTC),
    }
    event_values.update(values)

    with pytest.raises(ValueError):
        JobEvent(**event_values)  # type: ignore[arg-type]
