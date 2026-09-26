from __future__ import annotations

import pytest

from af.framework.errors import InvalidCharacter, InvalidPagination, InvalidPrompt, TaskNotFound
from af.framework.types import PROMPT_MAX_LENGTH, TASK_LIST_MAX_LIMIT, TASK_LIST_MAX_OFFSET


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


def test_count_characters_preserves_typed_arguments(application) -> None:  # type: ignore[no-untyped-def]
    text = "Line 1\nLine 2?"
    outcome = application.tasks.count_characters(text, "?")
    trace = application.traces.get(outcome.trace_id)

    assert outcome.prompt == text
    assert outcome.output == "1"
    assert outcome.route == "character-count"
    assert trace.prompt == "[redacted]"
    assert trace.arguments == {}


def test_count_characters_accepts_whitespace_text_and_character(
    application,
) -> None:  # type: ignore[no-untyped-def]
    outcome = application.tasks.count_characters("   ", " ")
    trace = application.traces.get(outcome.trace_id)

    assert outcome.prompt == "   "
    assert outcome.output == "3"
    assert trace.prompt == "[redacted]"
    assert trace.arguments == {}


def test_count_characters_accepts_exact_limit_and_rejects_overflow(
    application,
) -> None:  # type: ignore[no-untyped-def]
    assert application.tasks.count_characters("a" * PROMPT_MAX_LENGTH, "a").output == str(
        PROMPT_MAX_LENGTH
    )

    with pytest.raises(
        InvalidPrompt,
        match=f"prompt must be at most {PROMPT_MAX_LENGTH} characters",
    ):
        application.tasks.count_characters("a" * (PROMPT_MAX_LENGTH + 1), "a")


def test_count_characters_rejects_invalid_character(application) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(InvalidCharacter, match="exactly one code point"):
        application.tasks.count_characters("strawberry", "rr")


def test_task_list_rejects_invalid_pagination_bounds(application) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(
        InvalidPagination,
        match=f"limit must be between 1 and {TASK_LIST_MAX_LIMIT}",
    ):
        application.tasks.list(limit=TASK_LIST_MAX_LIMIT + 1)

    with pytest.raises(
        InvalidPagination,
        match=f"offset must be between 0 and {TASK_LIST_MAX_OFFSET}",
    ):
        application.tasks.list(offset=TASK_LIST_MAX_OFFSET + 1)


def test_task_list_rejects_non_exact_pagination_primitives(application) -> None:  # type: ignore[no-untyped-def]
    class HostileInt(int):
        def __lt__(self, other: object) -> bool:
            raise RuntimeError("SECRET-limit-comparison")

        def __gt__(self, other: object) -> bool:
            raise RuntimeError("SECRET-limit-comparison")

    with pytest.raises(InvalidPagination, match="limit must be an integer"):
        application.tasks.list(limit=True)

    with pytest.raises(InvalidPagination, match="limit must be an integer"):
        application.tasks.list(limit=HostileInt(1))

    with pytest.raises(InvalidPagination, match="offset must be an integer"):
        application.tasks.list(offset=HostileInt(0))
