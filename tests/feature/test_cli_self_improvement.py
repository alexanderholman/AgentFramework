from __future__ import annotations

import json
from datetime import UTC, datetime

import typer
from typer.testing import CliRunner

from af.infrastructure.database import Database
from af.infrastructure.self_operation_fixtures import (
    create_fixture_self_improvement_service,
    reference_evolution_snapshot,
)
from af.interfaces.self_improvement import register_self_improvement_commands


def test_self_improvement_requires_explicit_fixture_or_configured_service(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'improvement.db'}")
    calls = []

    def configured(context):
        calls.append("configured")
        raise ValueError("self-improvement stage ports are not configured")

    def fixture(context):
        calls.append("fixture")
        return create_fixture_self_improvement_service(database, authentication_key=b"k" * 32)

    cli = typer.Typer()
    register_self_improvement_commands(
        cli, configured, fixture, lambda: reference_evolution_snapshot(now=datetime.now(UTC))
    )
    runner = CliRunner()
    assert runner.invoke(cli, ["self-improve"]).exit_code == 2
    result = runner.invoke(cli, ["self-improve", "--fixture", "--operation-id", "fixture-test"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["reason"] == "complete"
    assert calls == ["configured", "fixture"]
    result = runner.invoke(cli, ["self-improve", "--fixture", "--operation-id", "fixture-test"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["reason"] == "complete"
    database.close()


def test_evolve_uses_actual_typed_snapshot_and_explicit_fixture(tmp_path):
    cli = typer.Typer()
    fixed = reference_evolution_snapshot(now=datetime(2026, 9, 26, tzinfo=UTC))
    register_self_improvement_commands(cli, lambda _: None, lambda _: None, lambda: fixed)
    runner = CliRunner()
    assert runner.invoke(cli, ["evolve"]).exit_code == 2
    fixture = runner.invoke(cli, ["evolve", "--fixture"])
    assert fixture.exit_code == 0
    assert json.loads(fixture.stdout)["proposal_digest"]
    path = tmp_path / "snapshot.json"
    path.write_text(fixed.model_dump_json())
    actual = runner.invoke(cli, ["evolve", "--snapshot", str(path)])
    assert actual.exit_code == 0
    assert not json.loads(actual.stdout)["fixture"]
    assert (
        json.loads(actual.stdout)["proposal_digest"]
        == json.loads(fixture.stdout)["proposal_digest"]
    )
    assert runner.invoke(cli, ["evolve", "--snapshot", str(path), "--fixture"]).exit_code == 2
    path.write_bytes(b"x" * 1_048_577)
    assert runner.invoke(cli, ["evolve", "--snapshot", str(path)]).exit_code == 2
