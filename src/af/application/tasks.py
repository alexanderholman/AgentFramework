"""Task use cases; every transport delegates here."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

from af.domain.tasks import Task, TaskRepository
from af.framework.contracts import Agent
from af.framework.errors import InvalidCharacter, InvalidPagination, InvalidPrompt
from af.framework.types import (
    PROMPT_MAX_LENGTH,
    TASK_LIST_MAX_LIMIT,
    TASK_LIST_MAX_OFFSET,
    Classification,
    ExecutionOutcome,
    WorkRequest,
    normalize_prompt,
)


class TaskService:
    def __init__(self, agent: Agent, repository: TaskRepository) -> None:
        self._agent = agent
        self._repository = repository

    def execute(self, prompt: str) -> ExecutionOutcome:
        normalized = self._normalize(prompt)
        return self._agent.handle(WorkRequest(prompt=normalized))

    def create(self, prompt: str) -> Task:
        normalized = self._normalize(prompt)
        outcome = self._agent.handle(WorkRequest(prompt=normalized))
        now = datetime.now(UTC)
        return self._repository.save(
            Task(
                id=str(uuid4()),
                prompt=normalized,
                output=outcome.output,
                intent=outcome.intent,
                route=outcome.route,
                tier=outcome.tier,
                status=outcome.status,
                verified=outcome.verified,
                reused=outcome.reused,
                trace_id=outcome.trace_id,
                created_at=now,
                updated_at=now,
            )
        )

    def get(self, task_id: str) -> Task:
        return self._repository.get(task_id)

    def list(self, *, limit: int | None = None, offset: int = 0) -> list[Task]:
        normalized_limit, normalized_offset = self._normalize_list_window(limit, offset)
        return self._repository.list(limit=normalized_limit, offset=normalized_offset)

    def update(self, task_id: str, prompt: str) -> Task:
        existing = self._repository.get(task_id)
        normalized = self._normalize(prompt)
        outcome = self._agent.handle(WorkRequest(prompt=normalized))
        return self._repository.save(
            replace(
                existing,
                prompt=normalized,
                output=outcome.output,
                intent=outcome.intent,
                route=outcome.route,
                tier=outcome.tier,
                status=outcome.status,
                verified=outcome.verified,
                reused=outcome.reused,
                trace_id=outcome.trace_id,
                updated_at=datetime.now(UTC),
            )
        )

    def count_characters(self, text: str, character: str) -> ExecutionOutcome:
        validated_text = self._normalize_count_text(text)
        validated_character = self._normalize_character(character)
        return self._agent.handle_classification(
            WorkRequest(prompt=validated_text),
            Classification(
                intent="text.character_count",
                arguments={"text": validated_text, "character": validated_character},
                confidence=1.0,
                classifier=type(self).__name__,
            ),
            expected_procedure_id="character-count",
        )

    def delete(self, task_id: str) -> None:
        self._repository.delete(task_id)

    @staticmethod
    def _normalize(prompt: str) -> str:
        return normalize_prompt(prompt)

    @staticmethod
    def _normalize_list_window(limit: int | None, offset: int) -> tuple[int | None, int]:
        if limit is None:
            normalized_limit = None
        elif not isinstance(limit, int) or isinstance(limit, bool):
            raise InvalidPagination("limit must be an integer")
        elif limit < 1 or limit > TASK_LIST_MAX_LIMIT:
            raise InvalidPagination(f"limit must be between 1 and {TASK_LIST_MAX_LIMIT}")
        else:
            normalized_limit = limit
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise InvalidPagination("offset must be an integer")
        if offset < 0 or offset > TASK_LIST_MAX_OFFSET:
            raise InvalidPagination(f"offset must be between 0 and {TASK_LIST_MAX_OFFSET}")
        return normalized_limit, offset

    @staticmethod
    def _normalize_count_text(text: str) -> str:
        if not isinstance(text, str):
            raise InvalidPrompt("prompt must be a string")
        if not text:
            raise InvalidPrompt()
        if len(text) > PROMPT_MAX_LENGTH:
            raise InvalidPrompt(f"prompt must be at most {PROMPT_MAX_LENGTH} characters")
        return text

    @staticmethod
    def _normalize_character(character: str) -> str:
        if not isinstance(character, str) or len(character) != 1:
            raise InvalidCharacter("" if not isinstance(character, str) else character)
        return character


class TaskController:
    """MVC controller shared by presentation adapters."""

    def __init__(self, service: TaskService) -> None:
        self._service = service

    def execute(self, prompt: str) -> ExecutionOutcome:
        return self._service.execute(prompt)

    def create(self, prompt: str) -> Task:
        return self._service.create(prompt)

    def get(self, task_id: str) -> Task:
        return self._service.get(task_id)

    def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Task]:
        return self._service.list(limit=limit, offset=offset)

    def update(self, task_id: str, prompt: str) -> Task:
        return self._service.update(task_id, prompt)

    def count_characters(self, text: str, character: str) -> ExecutionOutcome:
        return self._service.count_characters(text, character)

    def delete(self, task_id: str) -> None:
        self._service.delete(task_id)
