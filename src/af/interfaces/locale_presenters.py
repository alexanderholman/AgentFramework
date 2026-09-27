"""Add locale presentation metadata without changing domain serialization."""

from __future__ import annotations

from af.application.locales import LocaleSelection, Localizer, Message
from af.domain.tasks import Task
from af.framework.types import ExecutionOutcome
from af.interfaces.presenters import outcome_to_dict, task_to_dict


def localize_outcome(
    outcome: ExecutionOutcome,
    localizer: Localizer,
    selection: LocaleSelection,
) -> dict[str, object]:
    payload = outcome_to_dict(outcome)
    payload["presentation"] = localizer.render(
        Message("result.completed", {"procedure": outcome.route, "output": outcome.output}),
        selection,
    ).to_dict()
    return payload


def localize_task(
    task: Task,
    localizer: Localizer,
    selection: LocaleSelection,
) -> dict[str, object]:
    payload = task_to_dict(task)
    payload["presentation"] = localizer.render(
        Message("result.completed", {"procedure": task.route or "", "output": task.output or ""}),
        selection,
    ).to_dict()
    return payload
