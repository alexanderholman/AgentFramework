"""Canonical relational search resources for persisted AF tasks."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from af.infrastructure.database import TaskRow
from af.search.contracts import ResourceEnvelope, ResourceKind

__all__ = ("task_row_to_resource",)

_SOURCE_ID = "af_tasks"
_LOCATOR_PREFIX = "af://relational/af_tasks/"


def task_row_to_resource(
    row: TaskRow,
    *,
    tenant_id: str,
    acl: tuple[str, ...],
    projection_version: int,
) -> ResourceEnvelope:
    """Project one persisted task row into the canonical relational search contract."""

    if not isinstance(row, TaskRow):
        raise TypeError("task search adapter requires a TaskRow")
    text = f"{row.prompt}\n\n{row.output}"
    created_at = _timestamp(row.created_at)
    updated_at = _timestamp(row.updated_at)
    attributes = {
        "intent": row.intent,
        "route": row.route,
        "tier": row.tier,
        "status": row.status,
        "verified": _bool_text(row.verified),
        "reused": _bool_text(row.reused),
        "trace_id": row.trace_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "table_name": _SOURCE_ID,
    }
    provenance = (_provenance(row=row, text=text, created_at=created_at, updated_at=updated_at),)
    return ResourceEnvelope(
        resource_id=row.id,
        kind=ResourceKind.RELATIONAL,
        source_id=_SOURCE_ID,
        authoritative_locator=f"{_LOCATOR_PREFIX}{row.id}",
        tenant_id=tenant_id,
        projection_version=projection_version,
        acl=acl,
        provenance=provenance,
        text=text,
        attributes=attributes,
    )


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _timestamp(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _provenance(
    *,
    row: TaskRow,
    text: str,
    created_at: str,
    updated_at: str,
) -> str:
    encoded = json.dumps(
        {
            "id": row.id,
            "prompt": row.prompt,
            "output": row.output,
            "intent": row.intent,
            "route": row.route,
            "tier": row.tier,
            "status": row.status,
            "verified": row.verified,
            "reused": row.reused,
            "trace_id": row.trace_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "text": text,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
