"""The configured schedule CLI feeds the actual durable queue and installed worker."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import typer
from typer.testing import CliRunner

from af.application.scheduling import ScheduledJobHandler, SchedulerService
from af.cache.database import DatabaseCacheDriver
from af.framework.scheduling import IntervalRule, ScheduleDefinition
from af.infrastructure.scheduling import (
    CacheSchedulerLocks,
    DurableSchedulerQueue,
    SQLiteScheduleHistory,
)
from af.infrastructure.sqlite import SQLiteDriver
from af.interfaces.scheduling import SchedulingCommands
from af.queue.database_manager import database_queue_registration
from af.queue.manager import QueueManager
from af.queue.worker import WorkerKernel


def test_source_controlled_schedule_cli_queues_and_executes_a_registered_task(tmp_path):
    now = datetime(2026, 9, 26, 12, tzinfo=UTC)

    def clock():
        return now

    definition = ScheduleDefinition(
        "count", "count", (("text", "hello"),), IntervalRule(timedelta(minutes=1), now), now
    )
    history = SQLiteScheduleHistory(tmp_path / "history.sqlite")
    cache = DatabaseCacheDriver(url=f"sqlite+pysqlite:///{tmp_path / 'locks.sqlite'}", clock=clock)
    queue = QueueManager(
        connections={"primary": {"driver": "database"}},
        default="primary",
        deployment_profile="test",
        policy_version="schedule-v1",
        clock=clock,
    )
    queue.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(tmp_path / "queue.sqlite"), clock=clock
        )
    )
    locks = CacheSchedulerLocks(cache)
    service = SchedulerService(
        (definition,), history, locks, DurableSchedulerQueue(queue), clock=clock, instance_id="test"
    )
    results = []
    handler = ScheduledJobHandler(
        (definition,), history, locks,
        {"count": lambda args, context: results.append(len(args["text"]))}, clock=clock,
    )
    worker = WorkerKernel(queue.connection(), clock=clock, driver_close=lambda: None)

    def work_once():
        reservation = worker.run_once(handler, lane="scheduler")
        return reservation.job.envelope.metadata.message_id if reservation is not None else None

    cli = typer.Typer()
    SchedulingCommands(service, history, work_once).register(cli)
    runner = CliRunner()
    try:
        listing = runner.invoke(cli, ["schedule", "list"])
        assert listing.exit_code == 0 and json.loads(listing.stdout)[0]["id"] == "count"
        preview = runner.invoke(cli, ["schedule", "dry-run"])
        assert preview.exit_code == 0 and json.loads(preview.stdout)[0]["state"] == "planned"
        assert queue.reserve(lane="scheduler") is None
        future = runner.invoke(cli, ["schedule", "next-due", "count"])
        assert future.exit_code == 0
        missing = runner.invoke(cli, ["schedule", "next-due", "missing"])
        assert missing.exit_code == 2
        dispatched = runner.invoke(cli, ["schedule", "tick"])
        assert dispatched.exit_code == 0
        assert json.loads(dispatched.stdout)[0]["state"] == "queued"
        execution = runner.invoke(cli, ["schedule", "work-once"])
        assert execution.exit_code == 0, execution.stdout
        assert json.loads(execution.stdout)["processed"] is True
        assert results == [5]
        history_cli = runner.invoke(cli, ["schedule", "history"])
        assert json.loads(history_cli.stdout)[0]["state"] == "completed"
        assert queue.reserve(lane="scheduler") is None
        empty = runner.invoke(cli, ["schedule", "work-once"])
        assert empty.exit_code == 0 and json.loads(empty.stdout)["processed"] is False
    finally:
        service.shutdown()
        worker.close(timeout=timedelta(seconds=5))
        queue.close()
        cache.close()


def test_scheduler_help_is_lazy_and_worker_callback_is_optional():
    from af.interfaces.scheduling import register_scheduling_commands

    def no_construction(context):
        raise AssertionError("help must not create scheduler resources")

    cli = typer.Typer()
    register_scheduling_commands(cli, no_construction)
    assert CliRunner().invoke(cli, ["schedule", "--help"]).exit_code == 0
