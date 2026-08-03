from __future__ import annotations

import pytest

from af.bootstrap import create_application
from af.config import Settings
from af.framework.errors import TaskNotFound, TraceNotFound


def test_sqlite_application_persists_tasks(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'af.sqlite3'}")
    first_application = create_application(settings=settings)
    created = first_application.tasks.create("how many rs in strawberry")

    second_application = create_application(settings=settings)
    reloaded = second_application.tasks.get(created.id)

    assert reloaded.output == "3"
    assert reloaded.trace_id == created.trace_id


def test_sqlite_task_repository_update_list_delete_and_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'tasks.sqlite3'}")
    application = create_application(settings=settings)
    created = application.tasks.create("how many rs in strawberry")

    updated = application.tasks.update(created.id, "how many bs in blueberry")

    assert updated.output == "2"
    assert application.tasks.list() == [updated]

    application.tasks.delete(created.id)
    assert application.tasks.list() == []
    with pytest.raises(TaskNotFound):
        application.tasks.get(created.id)
    with pytest.raises(TaskNotFound):
        application.tasks.delete(created.id)


def test_sqlite_task_repository_paginates_lists(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'paged.sqlite3'}")
    application = create_application(settings=settings)
    first = application.tasks.create("how many rs in strawberry")
    second = application.tasks.create("how many bs in blueberry")

    assert application.tasks.list(limit=1, offset=0) == [first]
    assert application.tasks.list(limit=1, offset=1) == [second]


def test_sqlite_operations_snapshot_reads_recent_tasks_in_descending_order(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'operations.sqlite3'}")
    application = create_application(settings=settings)
    first = application.tasks.create("HELLO")
    second = application.tasks.create("how many bs in blueberry")

    snapshot = application.operations.snapshot()

    assert [task.id for task in snapshot.recent_tasks] == [second.id, first.id]


def test_sqlite_trace_repository_is_persistent_and_append_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'traces.sqlite3'}")
    first_application = create_application(settings=settings)
    outcome = first_application.tasks.execute("how many rs in strawberry")
    original = first_application.traces.get(outcome.trace_id)

    second_application = create_application(settings=settings)

    assert second_application.traces.get(outcome.trace_id) == original
    assert second_application.traces.list() == [original]
    with pytest.raises(ValueError, match="trace is append-only"):
        second_application.traces.save(original)
    with pytest.raises(TraceNotFound):
        second_application.traces.get("missing-trace")
