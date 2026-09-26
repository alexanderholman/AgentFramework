"""Installed scheduling survives independent application invocations."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.interfaces.console import create_cli


def test_installed_schedule_queues_then_new_application_executes_and_records_task(tmp_path):
    origin = datetime.now(UTC) - timedelta(seconds=10)
    source = tmp_path / "schedules.json"
    source.write_text(
        json.dumps(
            {
                "schema": "af.schedules/1",
                "schedules": [
                    {
                        "id": "science-check",
                        "task_id": "task.create",
                        "arguments": {"prompt": "math add a=1/3 b=1/6"},
                        "starts_at": origin.isoformat(),
                        "rule": {
                            "kind": "interval",
                            "seconds": 86400,
                            "origin": origin.isoformat(),
                        },
                        "max_queue_wait_seconds": 300,
                    }
                ],
            }
        )
    )
    settings = Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path / 'af.sqlite'}", schedules_path=str(source)
    )
    first = create_application(settings=settings)
    runner = CliRunner()
    try:
        cli = create_cli(first)
        preview = runner.invoke(cli, ["schedule", "dry-run"])
        assert preview.exit_code == 0, preview.stdout
        assert json.loads(preview.stdout)[0]["state"] == "planned"
        queued = runner.invoke(cli, ["schedule", "tick"])
        assert queued.exit_code == 0, queued.stdout
        run = json.loads(queued.stdout)[0]
        assert run["state"] == "queued"
        assert first.tasks.list() == []
    finally:
        first.shutdown()

    second = create_application(settings=settings)
    try:
        cli = create_cli(second)
        worked = runner.invoke(cli, ["schedule", "work-once"])
        assert worked.exit_code == 0, worked.stdout
        assert json.loads(worked.stdout) == {"processed": True, "run_id": run["id"]}
        tasks = second.tasks.list()
        assert len(tasks) == 1 and tasks[0].verified
        assert json.loads(tasks[0].output)["quantity"]["exact"] == "1/2"
        history = runner.invoke(cli, ["schedule", "history"])
        assert history.exit_code == 0
        assert json.loads(history.stdout)[0]["state"] == "completed"
        again = runner.invoke(cli, ["schedule", "tick"])
        assert again.exit_code == 0 and json.loads(again.stdout) == []
        empty = runner.invoke(cli, ["schedule", "work-once"])
        assert empty.exit_code == 0 and json.loads(empty.stdout)["processed"] is False
        assert len(second.tasks.list()) == 1
    finally:
        second.shutdown()
