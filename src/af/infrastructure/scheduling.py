"""Persistent local scheduler history and CacheManager-backed fenced locks."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from af.cache.contracts import CacheKey, CacheNamespace, LockLease, LockOwner
from af.cache.manager import CacheDriver, CacheManager
from af.framework.scheduling import (
    CronRule,
    DstFoldPolicy,
    IntervalRule,
    MisfirePolicy,
    ScheduleDefinition,
    ScheduledRun,
    ScheduleError,
)
from af.queue.contracts import QueueDispatchReceipt, QueueJob
from af.queue.manager import QueueFeature, QueueManager


class CacheSchedulerLocks:
    """A shared lock-capable CacheManager is required for multiple hosts."""

    def __init__(
        self, cache: CacheManager | CacheDriver, *, namespace: CacheNamespace | None = None
    ) -> None:
        self._cache = cache
        self._namespace = namespace or CacheNamespace("af", "local", "scheduler")

    def acquire(self, key: str, owner: str, ttl: timedelta) -> LockLease | None:
        return self._cache.acquire_lock(self._namespace, CacheKey(key), LockOwner(owner), ttl=ttl)

    def renew(self, key: str, lease: LockLease, ttl: timedelta) -> LockLease | None:
        return self._cache.renew_lock(self._namespace, CacheKey(key), lease, ttl=ttl)

    def release(self, key: str, lease: LockLease) -> bool:
        return self._cache.release_lock(self._namespace, CacheKey(key), lease)


class SQLiteScheduleHistory:
    """Process-safe occurrence claims and compare-and-set lifecycle transitions."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS af_schedule_runs (
                id TEXT PRIMARY KEY, group_id TEXT NOT NULL, schedule_id TEXT NOT NULL,
                definition_digest TEXT NOT NULL, nominal_at TEXT NOT NULL, planned_at TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT NOT NULL
            )""")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS af_schedule_group_due "
                "ON af_schedule_runs(group_id, nominal_at)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _run(row: sqlite3.Row) -> ScheduledRun:
        return ScheduledRun(
            str(row["id"]),
            str(row["group_id"]),
            str(row["schedule_id"]),
            str(row["definition_digest"]),
            datetime.fromisoformat(row["nominal_at"]),
            datetime.fromisoformat(row["planned_at"]),
            str(row["state"]),
            str(row["reason"]),
        )

    def latest_nominal(self, group_id: str) -> datetime | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT nominal_at FROM af_schedule_runs WHERE group_id=? "
                "AND (state != 'failed' OR reason NOT IN ('dispatch_failed', 'lock_unavailable')) "
                "ORDER BY nominal_at DESC LIMIT 1",
                (group_id,),
            ).fetchone()
        return datetime.fromisoformat(row[0]) if row is not None else None

    def claim(self, run: ScheduledRun) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """INSERT INTO af_schedule_runs VALUES (?, ?, ?, ?, ?, ?, 'claimed', ?)
                ON CONFLICT(id) DO UPDATE SET state='claimed', reason=excluded.reason
                WHERE af_schedule_runs.state='failed'
                AND af_schedule_runs.reason IN ('dispatch_failed', 'lock_unavailable')""",
                (
                    run.id,
                    run.group_id,
                    run.schedule_id,
                    run.definition_digest,
                    run.nominal_at.isoformat(),
                    run.planned_at.isoformat(),
                    run.reason,
                ),
            )
            return cursor.rowcount == 1

    def transition(self, run_id: str, expected: tuple[str, ...], state: str, reason: str) -> bool:
        allowed = {
            "claimed",
            "queued",
            "running",
            "completed",
            "failed",
            "cancelled",
            "skipped",
            "expired",
        }
        if (
            not expected
            or set(expected) - allowed
            or state not in allowed
            or not reason
            or len(reason) > 100
        ):
            raise ScheduleError("schedule_invalid_transition")
        placeholders = ",".join("?" for _ in expected)
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE af_schedule_runs SET state=?, reason=? WHERE id=? "
                f"AND state IN ({placeholders})",
                (state, reason, run_id, *expected),
            )
            return cursor.rowcount == 1

    def get(self, run_id: str) -> ScheduledRun | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM af_schedule_runs WHERE id=?", (run_id,)
            ).fetchone()
        return self._run(row) if row is not None else None

    def list_runs(self, *, limit: int = 100) -> tuple[ScheduledRun, ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ScheduleError("schedule_invalid_history_limit")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM af_schedule_runs ORDER BY nominal_at, id LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._run(row) for row in rows)


class DurableSchedulerQueue:
    """Refuse inline drivers; the scheduler only admits asynchronous queue work."""

    durable = True

    def __init__(self, queues: QueueManager, *, name: str | None = None) -> None:
        self._queues = queues
        self._name = name

    def dispatch(self, job: QueueJob, *, name: str | None = None) -> QueueDispatchReceipt:
        selected = name or self._name
        self._queues.connection(
            selected, requires=(QueueFeature.RESERVATION, QueueFeature.RESERVATION_RENEWAL)
        )
        return self._queues.dispatch(job, name=selected)


def _configuration_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ScheduleError("schedule_invalid_configuration")
    return value


def _configuration_seconds(value: object) -> timedelta:
    if type(value) is not int:
        raise ScheduleError("schedule_duration_requires_integer_seconds")
    return timedelta(seconds=value)


def _unique_configuration_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ScheduleError("schedule_duplicate_configuration_key")
        values[key] = value
    return values


def load_schedule_definitions(path: Path) -> tuple[ScheduleDefinition, ...]:
    """Load source-controlled JSON; only installed task identifiers can be selected."""
    try:
        return _load_schedule_definitions(path)
    except ScheduleError:
        raise
    except (OSError, ValueError, TypeError, OverflowError) as error:
        raise ScheduleError("schedule_invalid_configuration") from error


def _load_schedule_definitions(path: Path) -> tuple[ScheduleDefinition, ...]:
    with path.open("rb") as source:
        content = source.read(1_048_577)
    if len(content) > 1_048_576:
        raise ScheduleError("schedule_configuration_too_large")
    try:
        payload = _configuration_object(
            json.loads(content, object_pairs_hook=_unique_configuration_pairs)
        )
    except (ValueError, RecursionError) as error:
        raise ScheduleError("schedule_invalid_configuration") from error
    if set(payload) != {"schema", "schedules"} or payload["schema"] != "af.schedules/1":
        raise ScheduleError("schedule_invalid_configuration")
    definitions = payload["schedules"]
    if not isinstance(definitions, list) or len(definitions) > 1000:
        raise ScheduleError("schedule_invalid_configuration")
    result: list[ScheduleDefinition] = []
    for item in definitions:
        data = _configuration_object(item)
        required = {"id", "task_id", "arguments", "rule", "starts_at"}
        allowed = required | {
            "misfire",
            "grace_seconds",
            "jitter_seconds",
            "max_catch_up",
            "one_server",
            "without_overlap",
            "max_runtime_seconds",
            "max_queue_wait_seconds",
            "version",
        }
        if not required <= set(data) <= allowed:
            raise ScheduleError("schedule_invalid_configuration")
        for key in ("id", "task_id", "starts_at"):
            if not isinstance(data[key], str):
                raise ScheduleError("schedule_invalid_configuration")
        args = _configuration_object(data["arguments"])
        if any(not isinstance(value, str) for value in args.values()):
            raise ScheduleError("schedule_invalid_configuration")
        source_rule = _configuration_object(data["rule"])
        rule: IntervalRule | CronRule
        if source_rule.get("kind") == "interval":
            if set(source_rule) != {"kind", "seconds", "origin"} or not isinstance(
                source_rule["origin"], str
            ):
                raise ScheduleError("schedule_invalid_configuration")
            rule = IntervalRule(
                _configuration_seconds(source_rule["seconds"]),
                datetime.fromisoformat(source_rule["origin"]),
            )
        elif source_rule.get("kind") == "cron":
            if (
                not {"kind", "expression"}
                <= set(source_rule)
                <= {"kind", "expression", "timezone", "fold_policy"}
            ):
                raise ScheduleError("schedule_invalid_configuration")
            rule = CronRule(
                str(source_rule["expression"]),
                str(source_rule.get("timezone", "UTC")),
                DstFoldPolicy(str(source_rule.get("fold_policy", "first"))),
            )
        else:
            raise ScheduleError("schedule_invalid_configuration")
        for name in ("jitter_seconds", "max_catch_up"):
            if name in data and type(data[name]) is not int:
                raise ScheduleError("schedule_invalid_configuration")
        for name in ("one_server", "without_overlap"):
            if name in data and type(data[name]) is not bool:
                raise ScheduleError("schedule_invalid_configuration")
        result.append(
            ScheduleDefinition(
                id=str(data["id"]),
                task_id=str(data["task_id"]),
                arguments=tuple((name, str(value)) for name, value in args.items()),
                rule=rule,
                starts_at=datetime.fromisoformat(str(data["starts_at"])),
                misfire=MisfirePolicy(str(data.get("misfire", "coalesce"))),
                grace=_configuration_seconds(data.get("grace_seconds", 5)),
                jitter_seconds=cast(int, data.get("jitter_seconds", 0)),
                max_catch_up=cast(int, data.get("max_catch_up", 32)),
                one_server=cast(bool, data.get("one_server", True)),
                without_overlap=cast(bool, data.get("without_overlap", True)),
                max_runtime=_configuration_seconds(data.get("max_runtime_seconds", 300)),
                max_queue_wait=_configuration_seconds(data.get("max_queue_wait_seconds", 300)),
                version=str(data.get("version", "1.0.0")),
            )
        )
    if len({item.id for item in result}) != len(result):
        raise ScheduleError("schedule_duplicate_definitions")
    return tuple(result)
