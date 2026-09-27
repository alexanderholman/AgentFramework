from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.interfaces.console import create_cli


def test_workflow_cli_resumes_after_application_restart(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}")
    first = create_application(settings=settings)
    runner = CliRunner()
    try:
        started = runner.invoke(
            create_cli(first),
            [
                "workflows",
                "start",
                '{"texts":["strawberry","raspberry"],"character":"r"}',
                "--max-nodes",
                "1",
            ],
        )
        assert started.exit_code == 0, started.output
        run_id = json.loads(started.output)["run_id"]
    finally:
        first.shutdown()
    second = create_application(settings=settings)
    try:
        cli = create_cli(second)
        finished = runner.invoke(cli, ["workflows", "resume", run_id])
        assert finished.exit_code == 0, finished.output
        data = json.loads(finished.output)
        assert data["complete"] is True
        assert data["outputs"][-1]["value"] == {"total": 6}
        repeated = runner.invoke(cli, ["workflows", "resume", run_id])
        assert json.loads(repeated.output)["revision"] == data["revision"]
    finally:
        second.shutdown()
