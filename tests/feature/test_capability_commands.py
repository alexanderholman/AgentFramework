from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from af.interfaces.console import create_cli


def test_capability_discovery_is_explicit_and_nonexecuting(application, tmp_path: Path) -> None:
    runner = CliRunner()
    cli = create_cli(application)
    result = runner.invoke(cli, ["capabilities", "--json"])
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["records"] == []
    assert snapshot["authorized_capability_ids"] == []
    assert snapshot["diagnostics"]
    catalogue = tmp_path / "catalogue.json"
    catalogue.write_text('{"schema_version":"1.0.0","records":[]}')
    result = runner.invoke(cli, ["capabilities", "--catalogue", str(catalogue), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["authorized_capability_ids"] == []
    request = tmp_path / "request.json"
    request.write_text('{"execute":"shell"}')
    result = runner.invoke(cli, ["capabilities", "--request", str(request)])
    assert result.exit_code == 2
    assert application.tasks.list() == []


def test_additional_contract_schemas_are_machine_readable(application) -> None:
    runner = CliRunner()
    cli = create_cli(application)
    for name in [
        "decision-request",
        "decision-result",
        "workflow-checkpoint",
        "capability-request",
        "evolution-snapshot",
    ]:
        result = runner.invoke(cli, ["schemas", name])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["type"] == "object"
