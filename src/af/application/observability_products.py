"""Optional observation use cases with redaction, sampling, bounds and access checks."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from math import isfinite, prod

from af.framework.context import Principal
from af.framework.observability_products import (
    InspectorEntry,
    InspectorKind,
    InspectorPolicy,
    InspectorStore,
    MetricCard,
    MetricDefinition,
    MetricKind,
    MetricsStore,
    ObservabilityDenied,
    ObservabilityUnavailable,
    SafeAttribute,
)


def _reference(value: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 200:
        raise ValueError("invalid observation scope or correlation")
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _authorize(principal: Principal, *, write: bool = False) -> None:
    scope = "af:observability:write" if write else "af:observability:read"
    if scope not in principal.scopes:
        raise ObservabilityDenied()


def _redact(
    fields: Mapping[str, object], names: tuple[str, ...]
) -> tuple[tuple[str, SafeAttribute], ...]:
    if len(fields) > 64:
        raise ValueError("too many inspection fields")
    safe: dict[str, SafeAttribute] = {}
    redacted = 0
    for key, value in fields.items():
        if (
            key in {"duration_ms", "bytes", "attempt", "count"}
            and type(value) in (int, float)
            and isinstance(value, (int, float))
            and isfinite(value)
            and 0 <= value <= 1e12
        ):
            safe[key] = value
        elif key == "status" and (
            (type(value) is int and isinstance(value, int) and 100 <= value <= 599)
            or value in ("ok", "error", "denied", "skipped", "cancelled")
        ):
            if isinstance(value, (str, int)):
                safe[key] = value
        elif key == "method" and value in (
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "HEAD",
            "OPTIONS",
        ):
            if isinstance(value, str):
                safe[key] = value
        elif key == "operation" and isinstance(value, str) and value in names:
            safe[key] = value
        else:
            redacted += 1
    if redacted:
        safe["redacted_fields"] = redacted
    return tuple(sorted(safe.items()))


class InspectorService:
    def __init__(
        self,
        store: InspectorStore,
        policy: InspectorPolicy,
        *,
        tenant_id: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store, self._policy, self._tenant, self._clock = (
            store,
            policy,
            _reference(tenant_id),
            clock,
        )

    @property
    def tenant_ref(self) -> str:
        return self._tenant

    def collect(
        self,
        kind: InspectorKind,
        name: str,
        *,
        event_id: str,
        fields: Mapping[str, object],
        tags: tuple[str, ...] = (),
        correlation_id: str | None = None,
    ) -> bool:
        """Best effort observation: a failure never changes the observed operation's result."""
        try:
            event = _reference(event_id)
            if (
                not isinstance(kind, InspectorKind)
                or name not in self._policy.names
                or len(tags) > 32
                or any(tag not in self._policy.tags for tag in tags)
            ):
                return False
            if int(event[7:23], 16) >= int(self._policy.sample_rate * 2**64):
                return False
            safe = _redact(fields, self._policy.names)
            now = self._clock()
            entry = InspectorEntry(
                event,
                self._tenant,
                kind,
                name,
                now,
                None if correlation_id is None else _reference(correlation_id),
                safe,
                tuple(sorted(set(tags))),
            )
            self._store.append(
                entry,
                cutoff=now - self._policy.retention_seconds,
                max_records=self._policy.max_records,
            )
        except Exception:
            return False
        return True

    def entries(
        self,
        principal: Principal,
        *,
        kind: InspectorKind | None = None,
        name: str | None = None,
        tag: str | None = None,
        limit: int = 100,
    ) -> tuple[InspectorEntry, ...]:
        _authorize(principal)
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or (kind is not None and not isinstance(kind, InspectorKind))
            or (name is not None and name not in self._policy.names)
            or (tag is not None and tag not in self._policy.tags)
        ):
            raise ValueError("invalid inspector query")
        try:
            return self._store.entries(
                self._tenant,
                since=self._clock() - self._policy.retention_seconds,
                kind=kind,
                name=name,
                tag=tag,
                limit=limit,
            )
        except Exception:
            raise ObservabilityUnavailable() from None

    def purge(self, principal: Principal) -> int:
        _authorize(principal, write=True)
        try:
            return self._store.purge(self._tenant)
        except Exception:
            raise ObservabilityUnavailable() from None


class MetricsService:
    def __init__(
        self,
        store: MetricsStore,
        definitions: tuple[MetricDefinition, ...],
        *,
        tenant_id: str,
        retention_seconds: int = 86400,
        max_rows: int = 10000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not 1 <= len(definitions) <= 32
            or len({item.name for item in definitions}) != len(definitions)
            or sum(prod(len(domain) for _, domain in item.label_domains) for item in definitions)
            > 1024
        ):
            raise ValueError("metric registry exceeds cardinality bound")
        if (
            type(retention_seconds) is not int
            or not 1 <= retention_seconds <= 2592000
            or type(max_rows) is not int
            or not 1 <= max_rows <= 10000
        ):
            raise ValueError("invalid metric retention")
        self._store, self._definitions, self._tenant, self._clock = (
            store,
            {item.name: item for item in definitions},
            _reference(tenant_id),
            clock,
        )
        self._retention, self._max_rows = retention_seconds, max_rows

    @property
    def tenant_ref(self) -> str:
        return self._tenant

    def record(self, name: str, value: float, labels: Mapping[str, str]) -> bool:
        definition = self._definitions.get(name)
        if (
            definition is None
            or type(value) not in (int, float)
            or not isfinite(value)
            or abs(value) > 1e12
        ):
            return False
        domains = dict(definition.label_domains)
        if (
            set(labels) != set(domains)
            or any(label not in domains[key] for key, label in labels.items())
            or (definition.kind is MetricKind.COUNTER and value < 0)
        ):
            return False
        try:
            now = self._clock()
            self._store.record(
                self._tenant,
                definition,
                tuple(sorted(labels.items())),
                value,
                now,
                cutoff=now - self._retention,
                max_rows=self._max_rows,
            )
        except Exception:
            return False
        return True

    def cards(self, principal: Principal) -> tuple[MetricCard, ...]:
        _authorize(principal)
        try:
            return self._store.cards(self._tenant, since=self._clock() - self._retention)
        except Exception:
            raise ObservabilityUnavailable() from None

    def purge(self, principal: Principal) -> int:
        _authorize(principal, write=True)
        try:
            return self._store.purge(self._tenant)
        except Exception:
            raise ObservabilityUnavailable() from None
