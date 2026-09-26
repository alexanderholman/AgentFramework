"""Optional inspector and metrics contracts; observational data is never task authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite, prod
from typing import Protocol

from af.framework.errors import AFError


class ObservabilityDenied(AFError):
    def __init__(self) -> None:
        super().__init__("observability access denied")


class ObservabilityUnavailable(AFError):
    def __init__(self) -> None:
        super().__init__("optional observability unavailable")


def identifier(value: str) -> None:
    if type(value) is not str or re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value) is None:
        raise ValueError("invalid observability identifier")


class InspectorKind(StrEnum):
    REQUEST = "request"
    JOB = "job"
    QUERY = "query"
    EVENT = "event"
    CACHE = "cache"
    LOG = "log"


@dataclass(frozen=True, slots=True)
class InspectorPolicy:
    names: tuple[str, ...]
    tags: tuple[str, ...] = ()
    sample_rate: float = 1.0
    max_records: int = 1000
    retention_seconds: int = 86400

    def __post_init__(self) -> None:
        if (
            type(self.names) is not tuple
            or not 1 <= len(self.names) <= 128
            or len(set(self.names)) != len(self.names)
        ):
            raise ValueError("inspector requires finite registered event names")
        if (
            type(self.tags) is not tuple
            or len(self.tags) > 32
            or len(set(self.tags)) != len(self.tags)
        ):
            raise ValueError("invalid inspector tag domain")
        for value in (*self.names, *self.tags):
            identifier(value)
        if (
            type(self.sample_rate) not in (int, float)
            or not isfinite(self.sample_rate)
            or not 0 <= self.sample_rate <= 1
        ):
            raise ValueError("invalid sample rate")
        if (
            type(self.max_records) is not int
            or not 1 <= self.max_records <= 10000
            or type(self.retention_seconds) is not int
            or not 1 <= self.retention_seconds <= 2592000
        ):
            raise ValueError("invalid inspector retention")


type SafeAttribute = str | int | float | bool


@dataclass(frozen=True, slots=True)
class InspectorEntry:
    event_ref: str
    tenant_ref: str
    kind: InspectorKind
    name: str
    observed_at: float
    correlation_ref: str | None
    attributes: tuple[tuple[str, SafeAttribute], ...]
    tags: tuple[str, ...]


class InspectorStore(Protocol):
    def append(self, entry: InspectorEntry, *, cutoff: float, max_records: int) -> None: ...
    def entries(
        self,
        tenant_ref: str,
        *,
        since: float,
        kind: InspectorKind | None,
        name: str | None,
        tag: str | None,
        limit: int,
    ) -> tuple[InspectorEntry, ...]: ...
    def purge(self, tenant_ref: str) -> int: ...


class MetricKind(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    kind: MetricKind
    label_domains: tuple[tuple[str, tuple[str, ...]], ...] = ()
    bounds: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        identifier(self.name)
        if (
            not isinstance(self.kind, MetricKind)
            or type(self.label_domains) is not tuple
            or len(self.label_domains) > 8
        ):
            raise ValueError("invalid metric definition")
        names = []
        for name, values in self.label_domains:
            identifier(name)
            if (
                type(values) is not tuple
                or not 1 <= len(values) <= 16
                or len(set(values)) != len(values)
            ):
                raise ValueError("metric labels require finite value domains")
            for value in values:
                identifier(value)
            names.append(name)
        if (
            len(set(names)) != len(names)
            or prod(len(values) for _, values in self.label_domains) > 256
        ):
            raise ValueError("metric cardinality exceeds limit")
        if (
            type(self.bounds) is not tuple
            or len(self.bounds) > 16
            or any(type(value) not in (int, float) or not isfinite(value) for value in self.bounds)
            or tuple(sorted(set(self.bounds))) != self.bounds
        ):
            raise ValueError("invalid histogram bounds")
        if (self.kind is MetricKind.HISTOGRAM) != bool(self.bounds):
            raise ValueError("histograms require finite ordered bounds")


@dataclass(frozen=True, slots=True)
class MetricCard:
    name: str
    kind: MetricKind
    labels: tuple[tuple[str, str], ...]
    count: int
    total: float
    minimum: float
    maximum: float
    latest: float
    bounds: tuple[float, ...]
    buckets: tuple[int, ...]


class MetricsStore(Protocol):
    def record(
        self,
        tenant_ref: str,
        definition: MetricDefinition,
        labels: tuple[tuple[str, str], ...],
        value: float,
        now: float,
        *,
        cutoff: float,
        max_rows: int,
    ) -> None: ...
    def cards(self, tenant_ref: str, *, since: float) -> tuple[MetricCard, ...]: ...
    def purge(self, tenant_ref: str) -> int: ...
