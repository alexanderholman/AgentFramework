"""Canonical relational search resources for persisted AF run traces."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from af.infrastructure.database import TraceRow
from af.search.contracts import ResourceEnvelope, ResourceKind

__all__ = ("trace_row_to_resource",)

_SOURCE_ID = "af_run_traces"
_LOCATOR_PREFIX = "af://relational/af_run_traces/"
_UNSAFE_DOCUMENT_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]")


def trace_row_to_resource(
    row: TraceRow,
    *,
    tenant_id: str,
    acl: tuple[str, ...],
    projection_version: int,
) -> ResourceEnvelope:
    """Project one persisted trace row into the canonical relational search contract."""

    if not isinstance(row, TraceRow):
        raise TypeError("trace search adapter requires a TraceRow")
    text = _document_text(row.prompt, row.output, row.arguments_json, row.events_json)
    created_at = _timestamp(row.created_at)
    attributes = {
        "intent": row.intent or "",
        "route": row.route or "",
        "tier": row.tier or "",
        "status": row.status,
        "verified": _bool_text(row.verified),
        "reused": _bool_text(row.reused),
        "regression_passed": _bool_text(row.regression_passed),
        "promoted": _bool_text(row.promoted),
        "created_at": created_at,
        "table_name": _SOURCE_ID,
    }
    provenance = (
        _provenance(
            row=row,
            text=text,
            created_at=created_at,
        ),
    )
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


def _document_text(prompt: str, output: str | None, arguments_json: str, events_json: str) -> str:
    return "\n\n".join(
        (
            _normalize_document_text(prompt),
            _normalize_document_text(output or ""),
            _normalize_document_text(arguments_json),
            _normalize_document_text(events_json),
        )
    )


def _normalize_document_text(value: str) -> str:
    return _UNSAFE_DOCUMENT_CHARACTER.sub(" ", value)


def _timestamp(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _provenance(
    *,
    row: TraceRow,
    text: str,
    created_at: str,
) -> str:
    encoded = json.dumps(
        {
            "id": row.id,
            "prompt": row.prompt,
            "intent": row.intent,
            "arguments_json": row.arguments_json,
            "route": row.route,
            "tier": row.tier,
            "output": row.output,
            "verified": row.verified,
            "reused": row.reused,
            "regression_passed": row.regression_passed,
            "promoted": row.promoted,
            "status": row.status,
            "events_json": row.events_json,
            "created_at": created_at,
            "text": text,
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
