from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from af.bootstrap import AFApplication
from af.interfaces.console import create_cli


def test_agent_introspection_reports_installed_contracts_without_executing(
    application: AFApplication,
) -> None:
    runner = CliRunner()
    cli = create_cli(application)
    before = application.traces.list()
    for command in (
        ["dev", "describe", "--json"],
        ["procedures", "--json"],
        ["providers", "--json"],
    ):
        result = runner.invoke(cli, command)
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)
    assert application.traces.list() == before
    routes = runner.invoke(cli, ["routes", "--json"])
    assert routes.exit_code == 0, routes.output
    assert {row["procedure_id"] for row in json.loads(routes.output)} >= {
        "hello",
        "character-count",
    }
    schema = runner.invoke(cli, ["schemas", "execution-request"])
    assert schema.exit_code == 0, schema.output
    assert "prompt" in json.loads(schema.output)["properties"]
    invalid = runner.invoke(cli, ["schemas", "unknown"])
    assert invalid.exit_code == 2


def test_doctor_checks_fresh_install_without_creating_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AF_DATABASE_URL", "sqlite+pysqlite:///missing/state.sqlite3")
    result = CliRunner().invoke(create_cli(), ["doctor", "--json"])
    assert result.exit_code == 1, result.output
    report = json.loads(result.output)
    assert report["ready"] is False
    assert report["checks"]["storage"] == "not_initialized"
    assert not (tmp_path / "missing").exists()
    assert "sqlite" not in result.output.lower()


def test_schemas_list_is_machine_readable(application: AFApplication) -> None:
    result = CliRunner().invoke(create_cli(application), ["schemas"])
    assert result.exit_code == 0, result.output
    assert {"execution-request", "procedure-proposal", "verification-result", "job-event"} <= set(
        json.loads(result.output)
    )
