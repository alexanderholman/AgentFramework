"""Source-controlled interval/cron schedules with explicit bounded temporal policies."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from af.cache.contracts import LockLease
from af.framework.errors import AFError


class ScheduleError(AFError, ValueError):
    pass


class DstFoldPolicy(StrEnum):
    FIRST = "first"
    SECOND = "second"
    BOTH = "both"


class MisfirePolicy(StrEnum):
    SKIP = "skip"
    COALESCE = "coalesce"
    CATCH_UP = "catch_up"


def aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ScheduleError("schedule_time_must_be_aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class IntervalRule:
    interval: timedelta
    origin: datetime

    def __post_init__(self) -> None:
        if not timedelta(seconds=1) <= self.interval <= timedelta(days=366):
            raise ScheduleError("schedule_interval_out_of_bounds")
        object.__setattr__(self, "origin", aware_utc(self.origin))

    def next_after(self, value: datetime) -> datetime:
        value = aware_utc(value)
        steps = max(0, (value - self.origin) // self.interval + 1)
        return self.origin + self.interval * steps

    def previous_at_or_before(self, value: datetime) -> datetime:
        value = aware_utc(value)
        return self.origin + self.interval * ((value - self.origin) // self.interval)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "interval",
            "seconds": self.interval.total_seconds(),
            "origin": self.origin.isoformat(),
        }


def _cron_field(text: str, minimum: int, maximum: int) -> frozenset[int]:
    values: set[int] = set()
    for entry in text.split(","):
        pieces = entry.split("/")
        if len(pieces) > 2:
            raise ScheduleError("schedule_invalid_cron")
        base = pieces[0]
        if len(pieces) == 2 and re.fullmatch(r"[0-9]+", pieces[1]) is None:
            raise ScheduleError("schedule_invalid_cron")
        step = int(pieces[1]) if len(pieces) == 2 else 1
        if not 1 <= step <= maximum + 1:
            raise ScheduleError("schedule_invalid_cron")
        if base == "*":
            first, last = minimum, maximum
        elif re.fullmatch(r"[0-9]+-[0-9]+", base):
            first, last = (int(item) for item in base.split("-"))
        elif re.fullmatch(r"[0-9]+", base):
            first, last = int(base), maximum if len(pieces) == 2 else int(base)
        else:
            raise ScheduleError("schedule_invalid_cron")
        if not minimum <= first <= last <= maximum:
            raise ScheduleError("schedule_invalid_cron")
        values.update(range(first, last + 1, step))
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class CronRule:
    expression: str
    timezone: str = "UTC"
    fold_policy: DstFoldPolicy = DstFoldPolicy.FIRST
    _fields: tuple[frozenset[int], ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.expression) > 200 or len(self.expression.split()) != 5:
            raise ScheduleError("schedule_invalid_cron")
        if not isinstance(self.fold_policy, DstFoldPolicy):
            raise ScheduleError("schedule_invalid_dst_policy")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ScheduleError("schedule_unknown_timezone") from error
        fields = tuple(
            _cron_field(text, minimum, maximum)
            for text, (minimum, maximum) in zip(
                self.expression.split(),
                ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7)),
                strict=True,
            )
        )
        object.__setattr__(self, "_fields", fields)

    def _matches(self, instant: datetime) -> bool:
        local = instant.astimezone(ZoneInfo(self.timezone))
        minute, hour, day, month, weekday = self._fields
        if local.minute not in minute or local.hour not in hour or local.month not in month:
            return False
        weekday_number = (local.weekday() + 1) % 7
        day_matches = local.day in day
        weekday_matches = weekday_number in weekday or (weekday_number == 0 and 7 in weekday)
        fields = self.expression.split()
        calendar_matches = (
            day_matches and weekday_matches
            if fields[2] == "*" or fields[4] == "*"
            else day_matches or weekday_matches
        )
        if not calendar_matches:
            return False
        ambiguous = local.replace(fold=0).utcoffset() != local.replace(fold=1).utcoffset()
        if ambiguous and self.fold_policy is not DstFoldPolicy.BOTH:
            return local.fold == (0 if self.fold_policy is DstFoldPolicy.FIRST else 1)
        return True

    def _search(self, start: datetime, direction: int) -> datetime:
        # Search UTC instants: nonexistent civil times cannot accidentally materialize.
        candidate = start
        for _ in range(366 * 24 * 60 + 1):
            if self._matches(candidate):
                return candidate
            candidate += timedelta(minutes=direction)
        raise ScheduleError("schedule_due_outside_366_day_horizon")

    def next_after(self, value: datetime) -> datetime:
        return self._search(
            aware_utc(value).replace(second=0, microsecond=0) + timedelta(minutes=1), 1
        )

    def previous_at_or_before(self, value: datetime) -> datetime:
        return self._search(aware_utc(value).replace(second=0, microsecond=0), -1)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": "cron",
            "expression": self.expression,
            "timezone": self.timezone,
            "fold_policy": self.fold_policy.value,
            "gap_policy": "skip",
            "horizon_days": 366,
        }


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    id: str
    task_id: str
    arguments: tuple[tuple[str, str], ...]
    rule: IntervalRule | CronRule
    starts_at: datetime
    misfire: MisfirePolicy = MisfirePolicy.COALESCE
    grace: timedelta = timedelta(seconds=5)
    jitter_seconds: int = 0
    max_catch_up: int = 32
    one_server: bool = True
    without_overlap: bool = True
    max_runtime: timedelta = timedelta(minutes=5)
    max_queue_wait: timedelta = timedelta(minutes=5)
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", tuple(tuple(item) for item in self.arguments))
        for value in (self.id, self.task_id):
            if re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value) is None:
                raise ScheduleError("schedule_invalid_identity")
        if not isinstance(self.rule, (IntervalRule, CronRule)) or not isinstance(
            self.misfire, MisfirePolicy
        ):
            raise ScheduleError("schedule_invalid_rule")
        if type(self.one_server) is not bool or type(self.without_overlap) is not bool:
            raise ScheduleError("schedule_invalid_lock_policy")
        if len(self.arguments) > 32 or len(dict(self.arguments)) != len(self.arguments):
            raise ScheduleError("schedule_invalid_arguments")
        for name, value in self.arguments:
            if (
                re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name) is None
                or not isinstance(value, str)
                or len(value) > 10000
            ):
                raise ScheduleError("schedule_invalid_arguments")
        if (
            type(self.max_catch_up) is not int
            or type(self.jitter_seconds) is not int
            or not 1 <= self.max_catch_up <= 32
            or not 0 <= self.jitter_seconds < 60
        ):
            raise ScheduleError("schedule_invalid_bound")
        if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", self.version) is None or len(self.version) > 32:
            raise ScheduleError("schedule_invalid_version")
        if (
            isinstance(self.rule, IntervalRule)
            and self.jitter_seconds >= self.rule.interval.total_seconds()
        ):
            raise ScheduleError("schedule_jitter_exceeds_interval")
        if not timedelta(0) <= self.grace <= timedelta(days=1):
            raise ScheduleError("schedule_invalid_grace")
        for duration in (self.max_runtime, self.max_queue_wait):
            if not timedelta(seconds=1) <= duration <= timedelta(days=1):
                raise ScheduleError("schedule_invalid_timeout")
        object.__setattr__(self, "starts_at", aware_utc(self.starts_at))

    @property
    def digest(self) -> str:
        payload = {
            "id": self.id,
            "task": self.task_id,
            "arguments": self.arguments,
            "rule": self.rule.to_dict(),
            "starts_at": self.starts_at.isoformat(),
            "misfire": self.misfire.value,
            "grace": self.grace.total_seconds(),
            "jitter": self.jitter_seconds,
            "catch_up": self.max_catch_up,
            "one_server": self.one_server,
            "without_overlap": self.without_overlap,
            "runtime": self.max_runtime.total_seconds(),
            "queue_wait": self.max_queue_wait.total_seconds(),
            "version": self.version,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def planned_at(self, nominal: datetime) -> datetime:
        if not self.jitter_seconds:
            return nominal
        value = hashlib.sha256(f"{self.id}:{nominal.isoformat()}".encode()).hexdigest()
        return nominal + timedelta(seconds=int(value[:8], 16) % self.jitter_seconds)


@dataclass(frozen=True, slots=True)
class ScheduledRun:
    id: str
    group_id: str
    schedule_id: str
    definition_digest: str
    nominal_at: datetime
    planned_at: datetime
    state: str = "planned"
    reason: str = "due"

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "schedule_id": self.schedule_id,
            "definition_digest": self.definition_digest,
            "nominal_at": self.nominal_at.isoformat(),
            "planned_at": self.planned_at.isoformat(),
            "state": self.state,
            "reason": self.reason,
        }


class ScheduleHistory(Protocol):
    def latest_nominal(self, group_id: str) -> datetime | None: ...
    def claim(self, run: ScheduledRun) -> bool: ...
    def transition(
        self, run_id: str, expected: tuple[str, ...], state: str, reason: str
    ) -> bool: ...
    def get(self, run_id: str) -> ScheduledRun | None: ...
    def list_runs(self, *, limit: int = 100) -> tuple[ScheduledRun, ...]: ...


class ScheduleLocks(Protocol):
    def acquire(self, key: str, owner: str, ttl: timedelta) -> LockLease | None: ...
    def renew(self, key: str, lease: LockLease, ttl: timedelta) -> LockLease | None: ...
    def release(self, key: str, lease: LockLease) -> bool: ...
