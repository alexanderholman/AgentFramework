"""Application boundary for bounded persisted-history search."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from af.domain.tasks import Task, TaskRepository
from af.framework.contracts import TraceRepository
from af.framework.errors import InvalidPagination
from af.framework.types import RunTrace, normalize_prompt
from af.search.contracts import ResourceEnvelope, ResourceKind, SearchFilter, SearchHit, SearchQuery
from af.search.manager import SearchManager

_INDEX_LIMIT_DEFAULT = 100
_INDEX_LIMIT_MAX = 1_000
_QUERY_LIMIT_DEFAULT = 20
_QUERY_LIMIT_MAX = 1_000
_PROJECTION_VERSION = 1
_TENANT_ID = "af-local"
_ACL = ("af:local",)
_TASK_SOURCE_ID = "af_tasks"
_TRACE_SOURCE_ID = "af_run_traces"
_TASK_LOCATOR_PREFIX = "af://relational/af_tasks/"
_TRACE_LOCATOR_PREFIX = "af://relational/af_run_traces/"
_UNSAFE_DOCUMENT_CHARACTER = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u2028\u2029]")


@dataclass(frozen=True, slots=True)
class SearchIndexSourceResult:
    source: str
    indexed: int


@dataclass(frozen=True, slots=True)
class SearchIndexResult:
    indexed: int
    sources: tuple[SearchIndexSourceResult, ...]


@dataclass(frozen=True, slots=True)
class SearchResult:
    items: tuple[SearchHit, ...]
    total: int


class SearchService:
    def __init__(
        self,
        manager: SearchManager,
        tasks: TaskRepository,
        traces: TraceRepository,
    ) -> None:
        self._manager = manager
        self._tasks = tasks
        self._traces = traces

    def index(self, *, limit: int = _INDEX_LIMIT_DEFAULT) -> SearchIndexResult:
        normalized_limit = _normalize_limit(limit, maximum=_INDEX_LIMIT_MAX)
        task_resources = tuple(
            _task_to_resource(task) for task in self._tasks.list(limit=normalized_limit, offset=0)
        )
        trace_resources = tuple(
            _trace_to_resource(trace) for trace in self._traces.list()[:normalized_limit]
        )
        task_count = self._manager.upsert(task_resources) if task_resources else 0
        trace_count = self._manager.upsert(trace_resources) if trace_resources else 0
        sources = (
            SearchIndexSourceResult(source="tasks", indexed=task_count),
            SearchIndexSourceResult(source="traces", indexed=trace_count),
        )
        return SearchIndexResult(
            indexed=task_count + trace_count,
            sources=sources,
        )

    def search(self, text: object, *, limit: int = _QUERY_LIMIT_DEFAULT) -> SearchResult:
        normalized_limit = _normalize_limit(limit, maximum=_QUERY_LIMIT_MAX)
        query = SearchQuery(
            text=normalize_prompt(text),
            filters=(
                SearchFilter("tenant_id", (_TENANT_ID,)),
                SearchFilter("acl", _ACL),
            ),
            limit=normalized_limit,
        )
        items = self._manager.search(query)
        return SearchResult(items=items, total=len(items))


class SearchController:
    def __init__(self, service: SearchService) -> None:
        self._service = service

    def index(self, *, limit: int = _INDEX_LIMIT_DEFAULT) -> SearchIndexResult:
        return self._service.index(limit=limit)

    def search(self, text: object, *, limit: int = _QUERY_LIMIT_DEFAULT) -> SearchResult:
        return self._service.search(text, limit=limit)


def _normalize_limit(value: int, *, maximum: int) -> int:
    if type(value) is not int:
        raise InvalidPagination("limit must be an integer")
    if value < 1 or value > maximum:
        raise InvalidPagination(f"limit must be between 1 and {maximum}")
    return value


def _task_to_resource(task: Task) -> ResourceEnvelope:
    text = _document_text(task.prompt, task.output)
    created_at = _timestamp(task.created_at)
    updated_at = _timestamp(task.updated_at)
    return ResourceEnvelope(
        resource_id=task.id,
        kind=ResourceKind.RELATIONAL,
        source_id=_TASK_SOURCE_ID,
        authoritative_locator=f"{_TASK_LOCATOR_PREFIX}{task.id}",
        tenant_id=_TENANT_ID,
        projection_version=_PROJECTION_VERSION,
        acl=_ACL,
        provenance=(
            _digest(
                {
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
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "text": text,
                }
            ),
        ),
        text=text,
        attributes={
            "intent": task.intent,
            "route": task.route,
            "tier": task.tier,
            "status": task.status,
            "verified": _bool_text(task.verified),
            "reused": _bool_text(task.reused),
            "trace_id": task.trace_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "table_name": _TASK_SOURCE_ID,
        },
    )


def _trace_to_resource(trace: RunTrace) -> ResourceEnvelope:
    text = _document_text(
        trace.prompt,
        trace.output or "",
        json.dumps(trace.arguments, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        json.dumps(
            [{"stage": event.stage, "detail": event.detail} for event in trace.events],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    created_at = _timestamp(trace.created_at)
    attributes = {
        "status": trace.status,
        "verified": _bool_text(trace.verified),
        "reused": _bool_text(trace.reused),
        "regression_passed": _bool_text(trace.regression_passed),
        "promoted": _bool_text(trace.promoted),
        "created_at": created_at,
        "table_name": _TRACE_SOURCE_ID,
    }
    for key, value in (("intent", trace.intent), ("route", trace.route), ("tier", trace.tier)):
        if value is not None:
            attributes[key] = value
    return ResourceEnvelope(
        resource_id=trace.id,
        kind=ResourceKind.RELATIONAL,
        source_id=_TRACE_SOURCE_ID,
        authoritative_locator=f"{_TRACE_LOCATOR_PREFIX}{trace.id}",
        tenant_id=_TENANT_ID,
        projection_version=_PROJECTION_VERSION,
        acl=_ACL,
        provenance=(
            _digest(
                {
                    "id": trace.id,
                    "prompt": trace.prompt,
                    "intent": trace.intent,
                    "arguments": trace.arguments,
                    "route": trace.route,
                    "tier": trace.tier,
                    "output": trace.output,
                    "verified": trace.verified,
                    "reused": trace.reused,
                    "regression_passed": trace.regression_passed,
                    "promoted": trace.promoted,
                    "status": trace.status,
                    "events": [
                        {"stage": event.stage, "detail": event.detail} for event in trace.events
                    ],
                    "created_at": created_at,
                    "text": text,
                }
            ),
        ),
        text=text,
        attributes=attributes,
    )


def _digest(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _bool_text(value: bool) -> str:
    return "true" if value else "false"


def _document_text(*parts: str) -> str:
    return "\n\n".join(_normalize_document_text(part) for part in parts)


def _normalize_document_text(value: str) -> str:
    return _UNSAFE_DOCUMENT_CHARACTER.sub(" ", value)


def _timestamp(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
