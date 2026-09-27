"""Fake-clock schedule semantics, distributed admission and lease lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from af.application.scheduling import ScheduledJobHandler, SchedulerService
from af.cache.memory import MemoryCacheDriver
from af.framework.context import CancellationToken
from af.framework.scheduling import (
    CronRule,
    DstFoldPolicy,
    IntervalRule,
    MisfirePolicy,
    ScheduleDefinition,
    ScheduleError,
)
from af.infrastructure.scheduling import CacheSchedulerLocks, SQLiteScheduleHistory
from af.queue.contracts import QueueDispatchDisposition, QueueDispatchReceipt


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Queue:
    durable = True

    def __init__(self, clock):
        self.clock = clock
        self.jobs = []
        self.fail = False

    def dispatch(self, job, *, name=None):
        if self.fail:
            raise RuntimeError("queue unavailable")
        self.jobs.append(job)
        return QueueDispatchReceipt(
            job.envelope.metadata.message_id,
            job.lane,
            QueueDispatchDisposition.ENQUEUED,
            self.clock(),
        )


def _services(tmp_path, *, policy=MisfirePolicy.COALESCE, jitter=0):
    start = datetime(2026, 9, 26, 12, tzinfo=UTC)
    clock = Clock(start)
    definition = ScheduleDefinition(
        id="demo",
        task_id="count",
        arguments=(("text", "hello"),),
        rule=IntervalRule(timedelta(minutes=1), start),
        starts_at=start,
        misfire=policy,
        jitter_seconds=jitter,
    )
    history = SQLiteScheduleHistory(tmp_path / "schedules.sqlite")
    driver = MemoryCacheDriver(clock=clock)
    locks = CacheSchedulerLocks(driver)
    queue = Queue(clock)
    first = SchedulerService((definition,), history, locks, queue, clock=clock, instance_id="first")
    second = SchedulerService(
        (definition,), history, locks, queue, clock=clock, instance_id="second"
    )
    return definition, clock, history, driver, locks, queue, first, second


def test_cron_dst_gap_skips_and_fold_policy_is_explicit():
    after = datetime(2026, 3, 29, 0, tzinfo=UTC)
    spring = CronRule("30 1 * * *", "Europe/London")
    assert spring.next_after(after) == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)
    before = datetime(2026, 10, 24, 23, tzinfo=UTC)
    first = CronRule("30 1 * * *", "Europe/London", DstFoldPolicy.FIRST)
    second = CronRule("30 1 * * *", "Europe/London", DstFoldPolicy.SECOND)
    both = CronRule("30 1 * * *", "Europe/London", DstFoldPolicy.BOTH)
    assert first.next_after(before) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert second.next_after(before) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert both.next_after(both.next_after(before)) == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)


def test_dry_plan_has_no_effect_then_two_instances_dispatch_once(tmp_path):
    _, _, history, _, _, queue, first, second = _services(tmp_path)
    assert len(first.plan()) == 1
    assert len(first.plan()) == 1
    assert history.list_runs() == () and queue.jobs == []
    first.tick()
    second.tick()
    assert len(queue.jobs) == 1
    assert history.list_runs()[0].state == "queued"


def test_without_overlap_survives_enqueue_and_releases_after_worker_completion(tmp_path):
    definition, clock, history, _, locks, queue, first, second = _services(tmp_path)
    first.tick()
    clock.now += timedelta(minutes=1)
    second.tick()
    assert len(queue.jobs) == 1
    seen = []
    worker = ScheduledJobHandler(
        (definition,),
        history,
        locks,
        {"count": lambda args, context: seen.append(args["text"])},
        clock=clock,
    )
    worker.invoke(queue.jobs[0], cancellation=CancellationToken())
    assert seen == ["hello"]
    clock.now += timedelta(minutes=1)
    second.tick()
    assert len(queue.jobs) == 2
    assert history.list_runs()[0].state == "completed"


@pytest.mark.parametrize(
    "policy,expected",
    [(MisfirePolicy.SKIP, 0), (MisfirePolicy.COALESCE, 1), (MisfirePolicy.CATCH_UP, 4)],
)
def test_misfires_are_explicit_and_catch_up_is_bounded(tmp_path, policy, expected):
    definition, clock, history, _, locks, queue, _, _ = _services(tmp_path, policy=policy)
    from dataclasses import replace

    definition = replace(definition, without_overlap=False, max_catch_up=4)
    service = SchedulerService(
        (definition,), history, locks, queue, clock=clock, instance_id="third"
    )
    clock.now += timedelta(minutes=10, seconds=30)
    service.tick()
    assert len(queue.jobs) == expected


def test_failed_dispatch_and_cancelled_execution_release_locks(tmp_path):
    definition, _, history, _, locks, queue, first, _ = _services(tmp_path)
    queue.fail = True
    first.tick()
    assert history.list_runs()[0].state == "failed"
    queue.fail = False
    first.tick()
    assert len(queue.jobs) == 1
    token = CancellationToken()
    token.cancel()
    worker = ScheduledJobHandler(
        (definition,),
        history,
        locks,
        {"count": lambda args, context: pytest.fail("cancelled work ran")},
        clock=first.clock,
    )
    worker.invoke(queue.jobs[0], cancellation=token)
    assert history.list_runs()[0].state == "cancelled"


def test_maintenance_jitter_and_shutdown_are_reproducible(tmp_path):
    _, clock, history, _, _, queue, first, _ = _services(tmp_path, jitter=10)
    initial = first.next_due("demo")
    assert initial == first.next_due("demo")
    assert clock.now <= initial < clock.now + timedelta(seconds=10)
    clock.now += timedelta(seconds=10)
    first.tick(maintenance=True)
    assert queue.jobs == [] and history.list_runs()[0].reason == "maintenance"
    first.shutdown()
    clock.now += timedelta(minutes=1)
    assert first.tick() == ()


@pytest.mark.parametrize(
    "expression", ["* * * *", "61 * * * *", "*/0 * * * *", "* * * * *; ls", "* * * * MON"]
)
def test_cron_is_bounded_numeric_syntax(expression):
    with pytest.raises(ScheduleError):
        CronRule(expression)


def test_concurrent_scheduler_instances_claim_occurrence_atomically(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    _, _, history, _, _, queue, first, second = _services(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda service: service.tick(), (first, second)))
    assert len(queue.jobs) == 1
    assert len(history.list_runs()) == 1


def test_expired_queued_occurrence_cannot_execute_after_a_new_lease(tmp_path):
    definition, clock, history, _, locks, queue, first, _ = _services(tmp_path)
    first.tick()
    old = queue.jobs[0]
    clock.now += timedelta(minutes=12)
    first.tick()
    assert len(queue.jobs) == 2
    seen = []
    handler = ScheduledJobHandler(
        (definition,),
        history,
        locks,
        {"count": lambda args, context: seen.append(context.run_id)},
        clock=clock,
    )
    handler(old)
    assert seen == [] and history.get(old.envelope.metadata.message_id).state == "expired"
    handler(queue.jobs[1])
    assert len(seen) == 1
    handler(queue.jobs[1])
    assert len(seen) == 1


def test_failed_task_releases_its_fenced_lock_and_records_failure(tmp_path):
    definition, clock, history, _, locks, queue, first, _ = _services(tmp_path)
    first.tick()

    def fail(args, context):
        raise RuntimeError("task failed")

    handler = ScheduledJobHandler((definition,), history, locks, {"count": fail}, clock=clock)
    with pytest.raises(RuntimeError):
        handler(queue.jobs[0])
    assert history.list_runs()[0].state == "failed"
    clock.now += timedelta(minutes=1)
    first.tick()
    assert len(queue.jobs) == 2


def test_cron_numeric_ranges_steps_weekdays_and_interval_boundaries():
    rule = CronRule("*/15 9-17 * * 1-5")
    friday = datetime(2026, 9, 25, 17, 45, tzinfo=UTC)
    assert rule.next_after(friday) == datetime(2026, 9, 28, 9, tzinfo=UTC)
    interval = IntervalRule(timedelta(seconds=30), friday)
    assert interval.next_after(friday - timedelta(days=1)) == friday
    assert interval.next_after(friday) == friday + timedelta(seconds=30)
    with pytest.raises(ScheduleError):
        IntervalRule(timedelta(0), friday)
    with pytest.raises(ScheduleError):
        CronRule("* * * * *", "Unknown/Nowhere")
    with pytest.raises(ScheduleError):
        CronRule("* * * * *", fold_policy="anything")


def test_declarative_source_loader_rejects_executable_extra_fields(tmp_path):
    import json

    from af.infrastructure.scheduling import load_schedule_definitions

    path = tmp_path / "schedules.json"
    item = {
        "id": "demo",
        "task_id": "count",
        "arguments": {"text": "hello"},
        "starts_at": "2026-09-26T12:00:00+00:00",
        "rule": {"kind": "interval", "seconds": 60, "origin": "2026-09-26T12:00:00+00:00"},
    }
    path.write_text(json.dumps({"schema": "af.schedules/1", "schedules": [item]}))
    loaded = load_schedule_definitions(path)
    assert loaded[0].task_id == "count"
    item["command"] = "__import__('os').system('bad')"
    path.write_text(json.dumps({"schema": "af.schedules/1", "schedules": [item]}))
    with pytest.raises(ScheduleError):
        load_schedule_definitions(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad/name"},
        {"task_id": "bad name"},
        {"rule": None},
        {"misfire": "guess"},
        {"one_server": "true"},
        {"without_overlap": 1},
        {"arguments": (("text", "a"), ("text", "b"))},
        {"arguments": (("bad key", "value"),)},
        {"arguments": (("text", "x" * 10001),)},
        {"jitter_seconds": 60},
        {"max_catch_up": 33},
        {"max_catch_up": True},
        {"grace": timedelta(seconds=-1)},
        {"max_runtime": timedelta(0)},
        {"max_queue_wait": timedelta(days=2)},
        {"version": "invalid"},
        {"starts_at": datetime(2026, 1, 1)},
    ],
)
def test_schedule_definition_domains_are_enforced(tmp_path, changes):
    from dataclasses import replace

    definition, *_ = _services(tmp_path)
    with pytest.raises(ScheduleError):
        replace(definition, **changes)


def test_scheduler_requires_durable_queue_and_unique_sources(tmp_path):
    definition, clock, history, _, locks, queue, _, _ = _services(tmp_path)
    with pytest.raises(ScheduleError):
        SchedulerService(
            (definition, definition), history, locks, queue, clock=clock, instance_id="bad"
        )
    queue.durable = False
    with pytest.raises(ScheduleError):
        SchedulerService((definition,), history, locks, queue, clock=clock, instance_id="bad")


def test_hooks_dry_tick_future_due_and_history_bounds(tmp_path):
    definition, clock, history, _, locks, queue, _, _ = _services(tmp_path)
    seen = []

    def observe(run, stage):
        seen.append(stage)
        if stage == "after_dispatch":
            raise RuntimeError("observer failure must not retry dispatched work")

    service = SchedulerService(
        (definition,), history, locks, queue, clock=clock, instance_id="observer", hooks=(observe,)
    )
    assert service.tick(dry_run=True) and not queue.jobs
    assert service.next_due(
        "demo", after=clock.now + timedelta(seconds=1)
    ) == clock.now + timedelta(minutes=1)
    service.tick()
    assert seen == ["before_dispatch", "after_dispatch"]
    assert len(queue.jobs) == 1
    with pytest.raises(ScheduleError):
        history.list_runs(limit=0)
    with pytest.raises(ScheduleError):
        history.transition("missing", (), "bad", "invalid")


def test_worker_rejects_tampered_or_unregistered_jobs_before_execution(tmp_path):
    from dataclasses import replace

    from af.messaging.envelopes import MessageEnvelope

    definition, clock, history, _, locks, queue, first, _ = _services(tmp_path)
    first.tick()
    handler = ScheduledJobHandler((definition,), history, locks, {}, clock=clock)
    job = queue.jobs[0]
    for field, value in [
        ("schedule_id", "other"),
        ("definition_digest", "wrong"),
        ("lease_owner", "wrong"),
        ("lease_key", "other"),
        ("fencing_token", "1"),
        ("lease_expires", 0),
    ]:
        payload = job.envelope.payload_copy()
        payload[field] = value
        modified = replace(
            job, envelope=MessageEnvelope.create(metadata=job.envelope.metadata, payload=payload)
        )
        with pytest.raises(ScheduleError):
            handler(modified)
    with pytest.raises(ScheduleError, match="task_not_installed"):
        handler(job)
    assert history.get(job.envelope.metadata.message_id).state == "failed"
    assert first.tick() == ()  # A failed execution is not silently retried.


@pytest.mark.parametrize(
    "changes",
    [
        {"id": 1},
        {"arguments": {"text": 3}},
        {"rule": {"kind": "exec"}},
        {"rule": {"kind": "interval", "seconds": True, "origin": "2026-01-01T00:00:00Z"}},
        {"rule": {"kind": "interval", "seconds": 60}},
        {"rule": {"kind": "cron", "expression": "* * * * *", "script": "code"}},
        {"jitter_seconds": "4"},
        {"one_server": "yes"},
        {"starts_at": "broken"},
    ],
)
def test_source_configuration_rejects_type_coercion_and_unknown_rules(tmp_path, changes):
    import json

    from af.infrastructure.scheduling import load_schedule_definitions

    item = {
        "id": "demo",
        "task_id": "count",
        "arguments": {},
        "starts_at": "2026-01-01T00:00:00Z",
        "rule": {"kind": "cron", "expression": "*/5 * * * *"},
    }
    item.update(changes)
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"schema": "af.schedules/1", "schedules": [item]}))
    with pytest.raises(ScheduleError):
        load_schedule_definitions(path)


@pytest.mark.parametrize(
    "source",
    [
        "[]",
        "{",
        "{}",
        '{"schema":"af.schedules/1","schedules":1}',
        '{"schema":1,"schema":2}',
        "x" * 1048577,
    ],
)
def test_source_file_structure_is_bounded(tmp_path, source):
    from af.infrastructure.scheduling import load_schedule_definitions

    path = tmp_path / "source.json"
    path.write_text(source)
    with pytest.raises(ScheduleError):
        load_schedule_definitions(path)


def test_source_cron_loads_and_duplicate_ids_fail(tmp_path):
    import json

    from af.infrastructure.scheduling import load_schedule_definitions

    item = {
        "id": "demo",
        "task_id": "count",
        "arguments": {},
        "starts_at": "2026-01-01T00:00:00Z",
        "rule": {"kind": "cron", "expression": "*/5 * * * *", "timezone": "Europe/London"},
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"schema": "af.schedules/1", "schedules": [item]}))
    assert isinstance(load_schedule_definitions(path)[0].rule, CronRule)
    path.write_text(json.dumps({"schema": "af.schedules/1", "schedules": [item, item]}))
    with pytest.raises(ScheduleError):
        load_schedule_definitions(path)


def test_cancellation_race_records_cancelled_even_when_callback_raises(tmp_path):
    definition, clock, history, _, locks, queue, first, _ = _services(tmp_path)
    first.tick()

    def cancelled_task(arguments, context):
        context.cancellation.cancel()
        raise ScheduleError("cancelled during invocation")

    handler = ScheduledJobHandler(
        (definition,), history, locks, {"count": cancelled_task}, clock=clock
    )
    with pytest.raises(ScheduleError):
        handler(queue.jobs[0])
    assert history.list_runs()[0].state == "cancelled"
    clock.now += timedelta(minutes=1)
    first.tick()
    assert len(queue.jobs) == 2
