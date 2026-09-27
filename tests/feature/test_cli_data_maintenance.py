from __future__ import annotations

import json

import typer
from typer.testing import CliRunner

from af.application.data_maintenance import DataMaintenanceController
from af.infrastructure.data_maintenance import SQLiteDataMaintenance
from af.infrastructure.database import Database
from af.interfaces.data_maintenance import register_data_commands


def test_cli_initialize_backup_restore_and_retention_preview(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    controller = DataMaintenanceController(
        SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    )
    cli = typer.Typer()
    register_data_commands(cli, lambda context: controller)
    runner = CliRunner()
    result = runner.invoke(cli, ["data", "init"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["verified"]
    backup = tmp_path / "backup.db"
    result = runner.invoke(cli, ["data", "backup", str(backup)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["sha256"]
    result = runner.invoke(cli, ["data", "restore", str(backup), str(tmp_path / "restored.db")])
    assert result.exit_code == 0
    result = runner.invoke(cli, ["data", "prune", "--before", "2026-01-01T00:00:00+00:00"])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["dry_run"]
    result = runner.invoke(
        cli, ["data", "prune", "--before", "2026-01-01T00:00:00+00:00", "--execute"]
    )
    assert result.exit_code == 0
    assert not json.loads(result.stdout)["dry_run"]
    result = runner.invoke(cli, ["data", "prune", "--before", "invalid"])
    assert result.exit_code == 2
    database.close()
