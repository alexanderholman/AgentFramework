"""Shared transport-neutral presentation mappings."""

from __future__ import annotations

from af.domain.tasks import Task
from af.framework.types import ExecutionOutcome


def task_to_dict(task: Task) -> dict[str, object]:
    return {
        "id": task.id,
        "prompt": task.prompt,
        "output": task.output,
        "intent": task.intent,
        "route": task.route,
        "tier": task.tier,
        "status": task.status,
        "verified": task.verified,
        "reused": task.reused,
        "trace_id": task.trace_id,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def delete_to_dict(task_id: str) -> dict[str, object]:
    return {"id": task_id, "deleted": True}


def outcome_to_dict(outcome: ExecutionOutcome) -> dict[str, object]:
    return {
        "prompt": outcome.prompt,
        "output": outcome.output,
        "intent": outcome.intent,
        "route": outcome.route,
        "tier": outcome.tier,
        "status": outcome.status,
        "verified": outcome.verified,
        "reused": outcome.reused,
        "trace_id": outcome.trace_id,
    }
