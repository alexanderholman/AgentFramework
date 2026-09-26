"""Thin, lazily constructed scheduler CLI for source-controlled schedules."""

from __future__ import annotations

import json
from collections.abc import Callable

import typer

from af.application.scheduling import SchedulerService
from af.framework.scheduling import ScheduleError, ScheduleHistory


class SchedulingCommands:
    def __init__(
        self,
        service: SchedulerService,
        history: ScheduleHistory,
        work_once: Callable[[], str | None] | None = None,
    ) -> None:
        self.service = service
        self.history = history
        self._work_once = work_once

    def register(self, cli: typer.Typer) -> None:
        register_scheduling_commands(cli, lambda _context: self)

    def work_once(self) -> str | None:
        if self._work_once is None:
            raise ScheduleError("schedule_worker_not_configured")
        return self._work_once()


def register_scheduling_commands(
    cli: typer.Typer,
    factory: Callable[[typer.Context], SchedulingCommands],
) -> None:
    """Register help without creating databases, queues, workers or applications."""
    group = typer.Typer(help="Inspect or dispatch configured schedules.", add_completion=False)

    @group.command("list")
    def list_schedules(context: typer.Context) -> None:
        commands = factory(context)
        payload = [
            {
                "id": item.id,
                "task_id": item.task_id,
                "digest": item.digest,
                "rule": item.rule.to_dict(),
                "misfire": item.misfire.value,
                "one_server": item.one_server,
                "without_overlap": item.without_overlap,
            }
            for item in commands.service.definitions()
        ]
        typer.echo(json.dumps(payload, sort_keys=True))

    @group.command("next-due")
    def next_due(context: typer.Context, schedule_id: str) -> None:
        try:
            due = factory(context).service.next_due(schedule_id)
        except ScheduleError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(2) from error
        typer.echo(json.dumps({"id": schedule_id, "next_due": due.isoformat()}))

    @group.command("dry-run")
    def dry_run(context: typer.Context, maintenance: bool = False) -> None:
        typer.echo(
            json.dumps(
                [item.to_dict() for item in factory(context).service.plan(maintenance=maintenance)],
                sort_keys=True,
            )
        )

    @group.command("tick")
    def tick(context: typer.Context, maintenance: bool = False) -> None:
        typer.echo(
            json.dumps(
                [item.to_dict() for item in factory(context).service.tick(maintenance=maintenance)],
                sort_keys=True,
            )
        )

    @group.command("history")
    def history(context: typer.Context, limit: int = typer.Option(100, min=1, max=1000)) -> None:
        typer.echo(
            json.dumps(
                [item.to_dict() for item in factory(context).history.list_runs(limit=limit)],
                sort_keys=True,
            )
        )

    @group.command("work-once")
    def work_once(context: typer.Context) -> None:
        try:
            run_id = factory(context).work_once()
        except ScheduleError as error:
            typer.echo(str(error), err=True)
            raise typer.Exit(2) from error
        typer.echo(json.dumps({"processed": run_id is not None, "run_id": run_id}))

    cli.add_typer(group, name="schedule")
