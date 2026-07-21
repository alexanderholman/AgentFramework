"""Append-style execution trace repositories."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select

from af.framework.errors import TraceNotFound
from af.framework.types import RunTrace, TraceEvent
from af.infrastructure.database import Database, TraceRow


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class InMemoryTraceRepository:
    def __init__(self) -> None:
        self._traces: dict[str, RunTrace] = {}

    def save(self, trace: RunTrace) -> None:
        if trace.id in self._traces:
            raise ValueError(f"trace is append-only: {trace.id}")
        self._traces[trace.id] = trace

    def get(self, trace_id: str) -> RunTrace:
        try:
            return self._traces[trace_id]
        except KeyError as error:
            raise TraceNotFound(trace_id) from error

    def list(self) -> list[RunTrace]:
        return sorted(self._traces.values(), key=lambda trace: trace.created_at)


class SqlAlchemyTraceRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def save(self, trace: RunTrace) -> None:
        with self._database.sessions.begin() as session:
            if session.get(TraceRow, trace.id) is not None:
                raise ValueError(f"trace is append-only: {trace.id}")
            session.add(
                TraceRow(
                    id=trace.id,
                    prompt=trace.prompt,
                    intent=trace.intent,
                    arguments_json=json.dumps(trace.arguments, sort_keys=True),
                    route=trace.route,
                    tier=trace.tier,
                    output=trace.output,
                    verified=trace.verified,
                    reused=trace.reused,
                    regression_passed=trace.regression_passed,
                    promoted=trace.promoted,
                    status=trace.status,
                    events_json=json.dumps(
                        [{"stage": event.stage, "detail": event.detail} for event in trace.events]
                    ),
                    created_at=trace.created_at,
                )
            )

    def get(self, trace_id: str) -> RunTrace:
        with self._database.sessions() as session:
            row = session.get(TraceRow, trace_id)
            if row is None:
                raise TraceNotFound(trace_id)
            return self._to_domain(row)

    def list(self) -> list[RunTrace]:
        with self._database.sessions() as session:
            rows = session.scalars(select(TraceRow).order_by(TraceRow.created_at)).all()
            return [self._to_domain(row) for row in rows]

    @staticmethod
    def _to_domain(row: TraceRow) -> RunTrace:
        event_values = json.loads(row.events_json)
        argument_values = json.loads(row.arguments_json)
        return RunTrace(
            id=row.id,
            prompt=row.prompt,
            intent=row.intent,
            arguments={str(key): str(value) for key, value in argument_values.items()},
            route=row.route,
            tier=row.tier,
            output=row.output,
            verified=row.verified,
            reused=row.reused,
            regression_passed=row.regression_passed,
            promoted=row.promoted,
            status=row.status,
            events=tuple(
                TraceEvent(stage=str(item["stage"]), detail=str(item["detail"]))
                for item in event_values
            ),
            created_at=_aware(row.created_at),
        )
