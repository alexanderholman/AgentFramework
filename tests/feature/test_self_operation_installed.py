from __future__ import annotations

import json

from typer.testing import CliRunner

from af.interfaces.console import create_cli


def test_self_improvement_requires_explicit_fixture_or_configured_ports(application) -> None:
    runner = CliRunner()
    cli = create_cli(application)
    denied = runner.invoke(cli, ["self-improve"])
    assert denied.exit_code == 2
    first = runner.invoke(cli, ["self-improve", "--fixture", "--operation-id", "demo"])
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["fixture"] is True
    second = runner.invoke(cli, ["self-improve", "--fixture", "--operation-id", "demo"])
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["reason"] == json.loads(first.output)["reason"]
    proposal = runner.invoke(cli, ["evolve", "--fixture"])
    assert proposal.exit_code == 0, proposal.output
    assert json.loads(proposal.output)["authority"] == "proposal-only"
    assert application.tasks.list() == []


def test_installed_peer_demo_executes_real_local_procedure(application) -> None:
    runner = CliRunner()
    cli = create_cli(application)
    preview = runner.invoke(cli, ["peer-demo"])
    assert preview.exit_code == 0
    assert json.loads(preview.output)["execute"] is False
    run = runner.invoke(cli, ["peer-demo", "--execute"])
    assert run.exit_code == 0, run.output
    data = json.loads(run.output)
    assert data["verified"] is True
    assert data["output"] == "3"
    assert data["replay_rejected"] is True
    assert data["revocation_observed"] is True
