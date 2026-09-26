"""Independent optional SQLite inspector/metric stores and telemetry sink adapters."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from bisect import bisect_left
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from af.application.observability_products import InspectorService, MetricsService
from af.framework.observability_products import (
    InspectorEntry,
    InspectorKind,
    MetricCard,
    MetricDefinition,
    MetricKind,
)
from af.observability.signals import TelemetryKind, TelemetrySignal


class _SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path == ":memory:":
            raise ValueError("optional stores require a file-backed SQLite path")

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=5)) as db, db:
            yield db


class SQLiteInspectorStore(_SQLiteStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS af_inspector_entries (
                    event_ref TEXT NOT NULL, tenant_ref TEXT NOT NULL, kind TEXT NOT NULL,
                    name TEXT NOT NULL, observed_at REAL NOT NULL, correlation_ref TEXT,
                    attributes TEXT NOT NULL, tags TEXT NOT NULL,
                    PRIMARY KEY(event_ref,tenant_ref)
                );
                CREATE INDEX IF NOT EXISTS af_inspector_tenant_time
                ON af_inspector_entries(tenant_ref,observed_at);
            """)

    def append(self, entry: InspectorEntry, *, cutoff: float, max_records: int) -> None:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM af_inspector_entries WHERE tenant_ref=? AND observed_at<?",
                (entry.tenant_ref, cutoff),
            )
            db.execute(
                "INSERT OR IGNORE INTO af_inspector_entries VALUES(?,?,?,?,?,?,?,?)",
                (
                    entry.event_ref,
                    entry.tenant_ref,
                    entry.kind.value,
                    entry.name,
                    entry.observed_at,
                    entry.correlation_ref,
                    json.dumps(entry.attributes),
                    json.dumps(entry.tags),
                ),
            )
            db.execute(
                "DELETE FROM af_inspector_entries WHERE rowid IN (SELECT rowid FROM "
                "af_inspector_entries WHERE tenant_ref=? ORDER BY observed_at DESC,rowid DESC "
                "LIMIT -1 OFFSET ?)",
                (entry.tenant_ref, max_records),
            )
            db.execute(
                "DELETE FROM af_inspector_entries WHERE rowid IN (SELECT rowid FROM "
                "af_inspector_entries ORDER BY observed_at DESC,rowid DESC LIMIT -1 OFFSET "
                "10000)"
            )

    def entries(
        self,
        tenant_ref: str,
        *,
        since: float,
        kind: InspectorKind | None,
        name: str | None,
        tag: str | None,
        limit: int,
    ) -> tuple[InspectorEntry, ...]:
        clauses = ["tenant_ref=?", "observed_at>=?"]
        args: list[object] = [tenant_ref, since]
        for column, value in (("kind", None if kind is None else kind.value), ("name", name)):
            if value is not None:
                clauses.append(column + "=?")
                args.append(value)
        if tag is not None:
            clauses.append("EXISTS(SELECT 1 FROM json_each(tags) WHERE value=?)")
            args.append(tag)
        args.append(limit)
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM af_inspector_entries WHERE "
                + " AND ".join(clauses)
                + " ORDER BY observed_at DESC,rowid DESC LIMIT ?",
                args,
            ).fetchall()
        return tuple(
            InspectorEntry(
                row[0],
                row[1],
                InspectorKind(row[2]),
                row[3],
                row[4],
                row[5],
                tuple(tuple(item) for item in json.loads(row[6])),
                tuple(json.loads(row[7])),
            )
            for row in rows
        )

    def purge(self, tenant_ref: str) -> int:
        with self._connection() as db:
            return db.execute(
                "DELETE FROM af_inspector_entries WHERE tenant_ref=?", (tenant_ref,)
            ).rowcount


class SQLiteMetricsStore(_SQLiteStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS af_metric_definitions (
                    tenant_ref TEXT NOT NULL,name TEXT NOT NULL,definition TEXT NOT NULL,
                    PRIMARY KEY(tenant_ref,name)
                );
                CREATE TABLE IF NOT EXISTS af_metric_buckets (
                    tenant_ref TEXT NOT NULL,name TEXT NOT NULL,labels TEXT NOT NULL,
                    bucket_start INTEGER NOT NULL,count INTEGER NOT NULL,total REAL NOT NULL,
                    minimum REAL NOT NULL,maximum REAL NOT NULL,latest REAL NOT NULL,
                    buckets TEXT NOT NULL,
                    PRIMARY KEY(tenant_ref,name,labels,bucket_start)
                );
            """)

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
    ) -> None:
        canonical = json.dumps(asdict(definition), sort_keys=True, separators=(",", ":"))
        labels_json = json.dumps(labels, separators=(",", ":"))
        bucket = int(now // 60) * 60
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT definition FROM af_metric_definitions WHERE tenant_ref=? AND name=?",
                (tenant_ref, definition.name),
            ).fetchone()
            if row is not None and row[0] != canonical:
                raise ValueError("stored metric definition differs from installed registry")
            db.execute(
                "INSERT OR IGNORE INTO af_metric_definitions VALUES(?,?,?)",
                (tenant_ref, definition.name, canonical),
            )
            db.execute(
                "DELETE FROM af_metric_buckets WHERE tenant_ref=? AND bucket_start<?",
                (tenant_ref, int(cutoff // 60) * 60),
            )
            key = (tenant_ref, definition.name, labels_json, bucket)
            row = db.execute(
                "SELECT count,total,minimum,maximum,latest,buckets FROM af_metric_buckets "
                "WHERE tenant_ref=? AND name=? AND labels=? AND bucket_start=?",
                key,
            ).fetchone()
            counts = [0] * (len(definition.bounds) + 1) if row is None else json.loads(row[5])
            if definition.kind is MetricKind.HISTOGRAM:
                counts[bisect_left(definition.bounds, value)] += 1
            values = (
                (1, value, value, value, value)
                if row is None
                else (row[0] + 1, row[1] + value, min(row[2], value), max(row[3], value), value)
            )
            db.execute(
                "INSERT OR REPLACE INTO af_metric_buckets VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*key, *values, json.dumps(counts)),
            )
            db.execute(
                "DELETE FROM af_metric_buckets WHERE rowid IN (SELECT rowid FROM "
                "af_metric_buckets WHERE tenant_ref=? ORDER BY bucket_start DESC,rowid DESC "
                "LIMIT -1 OFFSET ?)",
                (tenant_ref, max_rows),
            )
            db.execute(
                "DELETE FROM af_metric_buckets WHERE rowid IN (SELECT rowid FROM "
                "af_metric_buckets ORDER BY bucket_start DESC,rowid DESC LIMIT -1 OFFSET "
                "10000)"
            )

    def cards(self, tenant_ref: str, *, since: float) -> tuple[MetricCard, ...]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT "
                "m.name,m.labels,m.count,m.total,m.minimum,m.maximum,"
                "m.latest,m.buckets,d.definition "
                "FROM af_metric_buckets m JOIN af_metric_definitions d ON "
                "m.tenant_ref=d.tenant_ref AND m.name=d.name WHERE m.tenant_ref=? AND "
                "m.bucket_start>=? ORDER BY m.bucket_start,m.name,m.labels",
                (tenant_ref, int(since // 60) * 60),
            ).fetchall()
        cards: dict[tuple[str, str], MetricCard] = {}
        for row in rows:
            key = (row[0], row[1])
            definition = json.loads(row[8])
            current = MetricCard(
                row[0],
                MetricKind(definition["kind"]),
                tuple(tuple(item) for item in json.loads(row[1])),
                row[2],
                row[3],
                row[4],
                row[5],
                row[6],
                tuple(definition["bounds"]),
                tuple(json.loads(row[7])),
            )
            previous = cards.get(key)
            if previous is not None:
                current = replace(
                    current,
                    count=previous.count + current.count,
                    total=previous.total + current.total,
                    minimum=min(previous.minimum, current.minimum),
                    maximum=max(previous.maximum, current.maximum),
                    buckets=tuple(
                        a + b for a, b in zip(previous.buckets, current.buckets, strict=True)
                    ),
                )
            cards[key] = current
        return tuple(cards[key] for key in sorted(cards))

    def purge(self, tenant_ref: str) -> int:
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            removed = db.execute(
                "DELETE FROM af_metric_buckets WHERE tenant_ref=?", (tenant_ref,)
            ).rowcount
            db.execute("DELETE FROM af_metric_definitions WHERE tenant_ref=?", (tenant_ref,))
            return removed


class InspectorTelemetrySink:
    def __init__(self, inspector: InspectorService) -> None:
        self._inspector = inspector

    def emit(self, signal: TelemetrySignal, /) -> None:
        if signal.context is not None and signal.context.tenant_ref not in (
            None,
            self._inspector.tenant_ref,
        ):
            return
        identity = hashlib.sha256(
            repr(
                (
                    signal.name,
                    signal.observed_at.isoformat(),
                    signal.attributes,
                    None if signal.context is None else signal.context.to_envelope(),
                )
            ).encode()
        ).hexdigest()
        kind = InspectorKind.LOG if signal.kind is TelemetryKind.LOG else InspectorKind.EVENT
        self._inspector.collect(
            kind,
            signal.name,
            event_id=identity,
            fields=dict(signal.attributes),
            correlation_id=None if signal.context is None else signal.context.correlation_id,
        )


class MetricsTelemetrySink:
    def __init__(self, metrics: MetricsService) -> None:
        self._metrics = metrics

    def emit(self, signal: TelemetrySignal, /) -> None:
        if signal.kind is not TelemetryKind.METRIC or (
            signal.context is not None
            and signal.context.tenant_ref not in (None, self._metrics.tenant_ref)
        ):
            return
        attributes = dict(signal.attributes)
        value = attributes.pop("value", None)
        if (
            type(value) not in (int, float)
            or not isinstance(value, (int, float))
            or any(type(label) is not str for label in attributes.values())
        ):
            return
        labels = {key: str(label) for key, label in attributes.items()}
        self._metrics.record(signal.name, value, labels)
