from pathlib import Path

import typer
from typer.testing import CliRunner

from af.application.engineering_controller import EngineeringController
from af.framework.context import ExecutionContext, use_context
from af.interfaces.engineering import register_engineering_commands


def test_unconfigured_engineering_is_explicit_and_caller_cannot_forge_route_history(tmp_path: Path):
    cli = typer.Typer()
    register_engineering_commands(cli, lambda _: EngineeringController(None))
    request = tmp_path / "request.json"
    request.write_text('{"texts":["one"]}')
    with use_context(ExecutionContext()):
        result = CliRunner().invoke(cli, ["engineer", "--input", str(request)])
    assert result.exit_code == 2
    assert "repository_not_configured" in result.output
    request.write_text('{"texts":["one"],"lower_status":"no-route-exhausted"}')
    with use_context(ExecutionContext()):
        result = CliRunner().invoke(cli, ["engineer", "--input", str(request)])
    assert result.exit_code == 2
    assert "Extra inputs are not permitted" in result.output
