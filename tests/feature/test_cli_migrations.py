from __future__ import annotations

import json
from datetime import UTC, datetime

from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.framework.container import ProviderRegistrar, ServiceProvider
from af.framework.database_manager import bind_database_manager
from af.framework.migration_provider import MigrationProvider
from af.framework.migrations import Migration, MigrationProbe
from af.framework.relational import DatabaseRow, DatabaseStatement
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.sqlite_manager import sqlite_registration
from af.infrastructure.tracing import InMemoryTraceRepository
from af.interfaces.cli import create_cli

_NOW = datetime(2026, 7, 24, 10, tzinfo=UTC)
runner = CliRunner()


class DatabaseProvider(ServiceProvider):
    provider_id = "test.database"

    def register(self, registrar: ProviderRegistrar) -> None:
        bind_database_manager(
            registrar,
            connections={"primary": {"driver": "sqlite", "database": ":memory:"}},
            default="primary",
            read="primary",
            write="primary",
            deployment_profile="test",
            policy_version="policy-v1",
            registrations=(sqlite_registration(clock=lambda: _NOW),),
            clock=lambda: _NOW,
        )


def _migration() -> Migration:
    return Migration(
        "20260724100000_create_operator_records",
        (DatabaseStatement("CREATE TABLE operator_records (id INTEGER PRIMARY KEY)"),),
        (DatabaseStatement("DROP TABLE operator_records"),),
        (
            MigrationProbe(
                DatabaseStatement(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'operator_records'",
                    read_only=True,
                ),
                (DatabaseRow(("name",), ("operator_records",)),),
            ),
        ),
    )


def _application():  # type: ignore[no-untyped-def]
    return create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(
            DatabaseProvider(),
            MigrationProvider(
                migrations=(_migration(),),
                connection="primary",
                owner_id="cli-test",
                clock=lambda: _NOW,
            ),
        ),
    )


def test_migration_cli_plans_applies_reports_verifies_and_reverses() -> None:
    application = _application()
    cli = create_cli(application)

    planned = runner.invoke(cli, ["migrations", "plan"])
    assert planned.exit_code == 0
    assert json.loads(planned.stdout) == {
        "direction": "up",
        "migration_ids": ["20260724100000_create_operator_records"],
        "count": 1,
        "dry_run": True,
    }

    applied = runner.invoke(cli, ["migrations", "up"])
    assert applied.exit_code == 0
    assert json.loads(applied.stdout)["migration_ids"] == ["20260724100000_create_operator_records"]

    status = runner.invoke(cli, ["migrations", "status"])
    assert status.exit_code == 0
    assert json.loads(status.stdout) == {
        "applied": ["20260724100000_create_operator_records"],
        "pending": [],
    }

    verified = runner.invoke(cli, ["migrations", "verify"])
    assert verified.exit_code == 0
    assert json.loads(verified.stdout) == {"verified": ["20260724100000_create_operator_records"]}

    reversed_result = runner.invoke(cli, ["migrations", "down", "--steps", "1"])
    assert reversed_result.exit_code == 0
    assert json.loads(reversed_result.stdout)["direction"] == "down"
    assert json.loads(runner.invoke(cli, ["migrations", "status"]).stdout)["applied"] == []
    application.shutdown()


def test_migration_cli_fails_closed_without_provider() -> None:
    application = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    result = runner.invoke(create_cli(application), ["migrations", "status"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "service is not bound: MigrationRunner\n"
    application.shutdown()


def test_migration_cli_rejects_unbounded_operator_inputs() -> None:
    application = _application()
    cli = create_cli(application)

    too_many = runner.invoke(cli, ["migrations", "up", "--limit", "10001"])
    no_steps = runner.invoke(cli, ["migrations", "down", "--steps", "0"])

    assert too_many.exit_code == 2
    assert no_steps.exit_code == 2
    application.shutdown()
