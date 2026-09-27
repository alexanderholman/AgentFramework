from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from af.domain.tasks import Task, TaskConflict, task_version
from af.infrastructure.database import Database
from af.infrastructure.repositories import InMemoryTaskRepository, SqlAlchemyTaskRepository


@pytest.mark.parametrize("persistent", [False, True])
def test_atomic_compare_and_swap_has_one_winner(tmp_path, persistent):
    database = Database(f"sqlite+pysqlite:///{tmp_path / 'tasks.db'}") if persistent else None
    repo = SqlAlchemyTaskRepository(database) if database else InMemoryTaskRepository()
    now = datetime.now(UTC)
    task = Task(
        "id",
        "hello",
        "Hello",
        "hello",
        "hello",
        "local",
        "completed",
        True,
        False,
        "trace",
        now,
        now,
    )
    repo.save(task)

    def change(index):
        try:
            repo.compare_and_save(replace(task, output=str(index)), expected=task)
            return True
        except TaskConflict:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(change, range(8))) == 1
    with pytest.raises(TaskConflict):
        repo.compare_and_delete(task)
    current = repo.get("id")
    assert task_version(current) != task_version(task)
    repo.compare_and_delete(current)
    if database:
        database.close()
