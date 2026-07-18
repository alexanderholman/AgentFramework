from __future__ import annotations

import pytest

from af.framework.errors import InvalidPrompt, TaskNotFound
from af.framework.types import PROMPT_MAX_LENGTH


def test_task_service_crud_reexecutes_updates(application) -> None:  # type: ignore[no-untyped-def]
    created = application.tasks.create("how many rs in strawberry")
    assert created.output == "3"
    assert application.tasks.get(created.id) == created
    assert application.tasks.list() == [created]

    updated = application.tasks.update(created.id, "how many bs in blueberry")
    assert updated.id == created.id
    assert updated.output == "2"
    assert updated.created_at == created.created_at
    assert updated.updated_at >= created.updated_at

    application.tasks.delete(created.id)
    with pytest.raises(TaskNotFound):
        application.tasks.get(created.id)


def test_blank_task_prompt_is_rejected(application) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidPrompt):
        application.tasks.create("   ")


def test_too_long_task_prompt_is_rejected(application) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(
        InvalidPrompt,
        match=f"prompt must be at most {PROMPT_MAX_LENGTH} characters",
    ):
        application.tasks.create("a" * (PROMPT_MAX_LENGTH + 1))
