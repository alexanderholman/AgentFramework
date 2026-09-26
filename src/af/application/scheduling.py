"""Queue-only scheduler admission and installed-task worker execution."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from af.cache.contracts import LockLease, LockOwner
from af.framework.context import CancellationToken
from af.framework.scheduling import (
    MisfirePolicy,
    ScheduleDefinition,
    ScheduledRun,
    ScheduleError,
    ScheduleHistory,
    ScheduleLocks,
    aware_utc,
)
from af.messaging.envelopes import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue.contracts import QueueDispatchReceipt, QueueJob


class ScheduleQueue(Protocol):
    durable: bool

    def dispatch(self, job: QueueJob, *, name: str | None = None) -> QueueDispatchReceipt: ...


type ScheduleHook = Callable[[ScheduledRun, str], None]


class SchedulerService:
    def __init__(
        self,
        schedules: tuple[ScheduleDefinition, ...],
        history: ScheduleHistory,
        locks: ScheduleLocks,
        queue: ScheduleQueue,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        instance_id: str,
        hooks: tuple[ScheduleHook, ...] = (),
    ) -> None:
        if len(schedules) > 1000 or len({item.id for item in schedules}) != len(schedules):
            raise ScheduleError("schedule_duplicate_or_excessive_definitions")
        LockOwner(instance_id)
        if not queue.durable:
            raise ScheduleError("schedule_requires_durable_queue")
        self._schedules = {item.id: item for item in schedules}
        self._history = history
        self._locks = locks
        self._queue = queue
        self.clock = clock
        self._instance_id = instance_id
        self._hooks = hooks
        self._shutdown = threading.Event()
        self._tick_lock = threading.Lock()

    def definitions(self) -> tuple[ScheduleDefinition, ...]:
        return tuple(self._schedules.values())

    def _group(self, definition: ScheduleDefinition) -> str:
        suffix = "shared" if definition.one_server else self._instance_id
        return f"{definition.id}:{definition.digest}:{suffix}"

    def next_due(self, schedule_id: str, *, after: datetime | None = None) -> datetime:
        definition = self._schedules.get(schedule_id)
        if definition is None:
            raise ScheduleError("schedule_not_found")
        instant = aware_utc(after if after is not None else self.clock())
        candidate = definition.rule.previous_at_or_before(instant)
        while candidate < definition.starts_at or definition.planned_at(candidate) < instant:
            candidate = definition.rule.next_after(
                max(candidate, definition.starts_at - timedelta(microseconds=1))
            )
        return definition.planned_at(candidate)

    def plan(self, *, maintenance: bool = False) -> tuple[ScheduledRun, ...]:
        now = aware_utc(self.clock())
        plans: list[ScheduledRun] = []
        for definition in self._schedules.values():
            group = self._group(definition)
            previous = self._history.latest_nominal(group)
            latest = definition.rule.previous_at_or_before(now)
            if definition.planned_at(latest) > now:
                latest = definition.rule.previous_at_or_before(latest - timedelta(microseconds=1))
            limit = definition.max_catch_up if definition.misfire is MisfirePolicy.CATCH_UP else 1
            occurrences: list[datetime] = []
            while len(occurrences) < limit and latest >= definition.starts_at:
                if previous is not None and latest <= previous:
                    break
                occurrences.append(latest)
                latest = definition.rule.previous_at_or_before(latest - timedelta(microseconds=1))
            for nominal in reversed(occurrences):
                planned = definition.planned_at(nominal)
                missed = now - planned > definition.grace
                reason = (
                    "maintenance"
                    if maintenance
                    else "misfire_skipped"
                    if missed and definition.misfire is MisfirePolicy.SKIP
                    else "misfire_coalesced"
                    if missed and definition.misfire is MisfirePolicy.COALESCE
                    else "due"
                )
                state = "skipped" if reason in {"maintenance", "misfire_skipped"} else "planned"
                run_id = hashlib.sha256(f"{group}:{nominal.isoformat()}".encode()).hexdigest()
                plans.append(
                    ScheduledRun(
                        run_id,
                        group,
                        definition.id,
                        definition.digest,
                        nominal,
                        planned,
                        state,
                        reason,
                    )
                )
        return tuple(sorted(plans, key=lambda run: (run.planned_at, run.id)))

    def _notify(self, run: ScheduledRun, stage: str) -> None:
        for hook in self._hooks:
            try:
                hook(run, stage)
            except Exception:
                # Hooks are observational: their failure cannot replay an accepted queue effect.
                continue

    def tick(self, *, dry_run: bool = False, maintenance: bool = False) -> tuple[ScheduledRun, ...]:
        if self._shutdown.is_set():
            return ()
        with self._tick_lock:
            plans = self.plan(maintenance=maintenance)
            if dry_run:
                return plans
            outcomes: list[ScheduledRun] = []
            for run in plans:
                if self._shutdown.is_set():
                    break
                if not self._history.claim(run):
                    continue
                if run.state == "skipped":
                    self._history.transition(run.id, ("claimed",), "skipped", run.reason)
                    outcomes.append(replace(run, state="skipped"))
                    continue
                definition = self._schedules[run.schedule_id]
                key = (
                    f"schedule:{definition.id}"
                    if definition.without_overlap
                    else f"occurrence:{run.id}"
                )
                ttl = definition.max_runtime + definition.max_queue_wait + timedelta(seconds=30)
                try:
                    lease = self._locks.acquire(key, run.id, ttl)
                except Exception:
                    self._history.transition(run.id, ("claimed",), "failed", "lock_unavailable")
                    outcomes.append(replace(run, state="failed", reason="lock_unavailable"))
                    continue
                if lease is None:
                    self._history.transition(run.id, ("claimed",), "skipped", "overlap")
                    outcomes.append(replace(run, state="skipped", reason="overlap"))
                    continue
                now = aware_utc(self.clock())
                payload = {
                    "schedule_id": definition.id,
                    "definition_digest": definition.digest,
                    "lease_key": key,
                    "lease_owner": lease.owner.value,
                    "fencing_token": lease.fencing_token,
                    "lease_expires": lease.expires_at.isoformat(),
                }
                envelope = MessageEnvelope.create(
                    metadata=MessageMetadata(
                        message_id=run.id,
                        message_type="af.schedule.execute",
                        schema_version=1,
                        kind=EnvelopeKind.JOB,
                        occurred_at=now,
                        deadline=now + definition.max_queue_wait + definition.max_runtime,
                    ),
                    payload=payload,
                )
                self._notify(run, "before_dispatch")
                try:
                    self._queue.dispatch(QueueJob(envelope, lane="scheduler", max_attempts=1))
                    self._history.transition(run.id, ("claimed",), "queued", run.reason)
                    self._notify(run, "after_dispatch")
                except Exception:
                    self._history.transition(run.id, ("claimed",), "failed", "dispatch_failed")
                    self._locks.release(key, lease)
                    self._notify(run, "dispatch_failed")
                current = self._history.get(run.id)
                if current is not None:
                    outcomes.append(current)
            return tuple(outcomes)

    def shutdown(self) -> None:
        """Stop new admissions; queued work remains owned by the queue worker lifecycle."""
        self._shutdown.set()


@dataclass(frozen=True, slots=True)
class ScheduledTaskContext:
    run_id: str
    fencing_token: int
    cancellation: CancellationToken
    deadline: datetime


type ScheduledTask = Callable[[Mapping[str, str], ScheduledTaskContext], None]


class ScheduledJobHandler:
    cooperative_cancellation = True

    def __init__(
        self,
        schedules: tuple[ScheduleDefinition, ...],
        history: ScheduleHistory,
        locks: ScheduleLocks,
        tasks: Mapping[str, ScheduledTask],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._schedules = {item.id: item for item in schedules}
        self._history = history
        self._locks = locks
        self._tasks = dict(tasks)
        self._clock = clock

    def __call__(self, job: QueueJob) -> None:
        self.invoke(job, cancellation=CancellationToken())

    def invoke(self, job: QueueJob, /, *, cancellation: CancellationToken) -> None:
        if job.envelope.metadata.message_type != "af.schedule.execute":
            raise ScheduleError("schedule_invalid_job")
        payload = job.envelope.payload_copy()
        if not isinstance(payload, dict) or set(payload) != {
            "schedule_id",
            "definition_digest",
            "lease_key",
            "lease_owner",
            "fencing_token",
            "lease_expires",
        }:
            raise ScheduleError("schedule_invalid_job")
        run_id = job.envelope.metadata.message_id
        run = self._history.get(run_id)
        if run is None:
            raise ScheduleError("schedule_unknown_run")
        definition = self._schedules.get(run.schedule_id)
        if (
            definition is None
            or payload["definition_digest"] != definition.digest
            or run.definition_digest != definition.digest
        ):
            raise ScheduleError("schedule_stale_definition")
        if payload["schedule_id"] != definition.id or payload["lease_owner"] != run_id:
            raise ScheduleError("schedule_invalid_job")
        expected_key = (
            f"schedule:{definition.id}" if definition.without_overlap else f"occurrence:{run.id}"
        )
        if (
            payload["lease_key"] != expected_key
            or type(payload["fencing_token"]) is not int
            or not isinstance(payload["lease_expires"], str)
        ):
            raise ScheduleError("schedule_invalid_job")
        key = expected_key
        lease = LockLease(
            LockOwner(run_id),
            payload["fencing_token"],
            datetime.fromisoformat(payload["lease_expires"]),
        )
        if not self._history.transition(run_id, ("claimed", "queued"), "running", "started"):
            return
        active_lease = lease
        stop = threading.Event()
        lease_mutex = threading.Lock()
        heartbeat: threading.Thread | None = None
        terminal = "failed"
        reason = "execution_failed"
        ttl = definition.max_runtime + timedelta(seconds=30)
        try:
            if cancellation.cancelled:
                terminal, reason = "cancelled", "cancelled_before_start"
                return
            if (
                aware_utc(self._clock())
                > job.envelope.metadata.occurred_at + definition.max_queue_wait
            ):
                terminal, reason = "expired", "queue_wait_expired"
                return
            renewed = self._locks.renew(key, active_lease, ttl)
            if renewed is None:
                terminal, reason = "expired", "lease_lost"
                return
            active_lease = renewed
            started = time.monotonic()

            def maintain() -> None:
                nonlocal active_lease
                while not stop.wait(min(5.0, ttl.total_seconds() / 3)):
                    if time.monotonic() - started >= definition.max_runtime.total_seconds():
                        cancellation.cancel()
                    with lease_mutex:
                        try:
                            current = self._locks.renew(key, active_lease, ttl)
                        except Exception:
                            cancellation.cancel()
                            return
                        if current is None:
                            cancellation.cancel()
                            return
                        active_lease = current

            heartbeat = threading.Thread(
                target=maintain, name=f"af-schedule-{definition.id}", daemon=True
            )
            heartbeat.start()
            task = self._tasks.get(definition.task_id)
            if task is None:
                raise ScheduleError("schedule_task_not_installed")
            context = ScheduledTaskContext(
                run_id,
                active_lease.fencing_token,
                cancellation,
                aware_utc(self._clock()) + definition.max_runtime,
            )
            task(dict(definition.arguments), context)
            terminal, reason = (
                ("cancelled", "cancelled") if cancellation.cancelled else ("completed", "completed")
            )
        except Exception:
            terminal, reason = "failed", "execution_failed"
            raise
        finally:
            stop.set()
            if heartbeat is not None:
                heartbeat.join(timeout=6)
            try:
                with lease_mutex:
                    self._locks.release(key, active_lease)
            finally:
                self._history.transition(run_id, ("running",), terminal, reason)
