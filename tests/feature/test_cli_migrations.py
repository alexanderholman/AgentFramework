from __future__ import annotations

import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

from af.bootstrap import create_application
from af.config import Settings
from af.framework.container import ProviderRegistrar, ServiceProvider
from af.framework.database_manager import bind_database_manager
from af.framework.errors import InvalidProviderConfiguration
from af.framework.migration_provider import MigrationProvider
from af.framework.migrations import Migration, MigrationProbe
from af.framework.relational import DatabaseRow, DatabaseStatement
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.sqlite_manager import sqlite_registration
from af.infrastructure.tracing import InMemoryTraceRepository
from af.interfaces import cli as cli_module
from af.interfaces.cli import create_cli

_NOW = datetime(2026, 7, 24, 10, tzinfo=UTC)
runner = CliRunner()


class DatabaseProvider(ServiceProvider):
    provider_id = "test.database"

    def __init__(self, database: str = ":memory:") -> None:
        self._database = database

    def register(self, registrar: ProviderRegistrar) -> None:
        bind_database_manager(
            registrar,
            connections={"primary": {"driver": "sqlite", "database": self._database}},
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


def _application(database: str = ":memory:"):  # type: ignore[no-untyped-def]
    return create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(
            DatabaseProvider(database),
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

    bootstrapped = runner.invoke(cli, ["migrations", "bootstrap"])
    assert bootstrapped.exit_code == 0
    assert json.loads(bootstrapped.stdout) == {"bootstrapped": True}

    planned = runner.invoke(cli, ["migrations", "plan"])
    assert planned.exit_code == 0
    assert json.loads(planned.stdout) == {
        "direction": "up",
        "migration_ids": ["20260724100000_create_operator_records"],
        "count": 1,
        "dry_run": True,
    }

    dry_run = runner.invoke(cli, ["migrations", "up", "--dry-run"])
    assert dry_run.exit_code == 0
    assert json.loads(dry_run.stdout)["dry_run"] is True

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

    planned_down = runner.invoke(cli, ["migrations", "plan", "--direction", "down"])
    assert planned_down.exit_code == 0
    assert json.loads(planned_down.stdout)["direction"] == "down"

    reversed_result = runner.invoke(cli, ["migrations", "down", "--steps", "1"])
    assert reversed_result.exit_code == 0
    assert json.loads(reversed_result.stdout)["direction"] == "down"
    assert json.loads(runner.invoke(cli, ["migrations", "status"]).stdout)["applied"] == []
    application.shutdown()


@pytest.mark.parametrize(
    "arguments",
    [
        ["migrations", "plan"],
        ["migrations", "status"],
        ["migrations", "verify"],
        ["migrations", "up", "--dry-run"],
        ["migrations", "down", "--dry-run"],
    ],
)
def test_migration_cli_inspection_does_not_bootstrap_metadata(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    database = tmp_path / "migration-inspection.sqlite3"
    application = _application(str(database))

    result = runner.invoke(create_cli(application), arguments)

    assert result.exit_code == 2
    with sqlite3.connect(database) as connection:
        metadata = connection.execute(
            """SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name IN ('af_migrations', 'af_migration_locks')
            ORDER BY name"""
        ).fetchall()
    assert metadata == []
    application.shutdown()


def test_installed_cli_loads_explicit_application_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "operator-profile.sqlite3"
    module = ModuleType("test_migration_operator_profile")

    def build_application():  # type: ignore[no-untyped-def]
        return _application(str(database))

    module.build_application = build_application  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = runner.invoke(
        create_cli(),
        [
            "--application-factory",
            "test_migration_operator_profile:build_application",
            "migrations",
            "bootstrap",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"bootstrapped": True}
    with sqlite3.connect(database) as connection:
        metadata = connection.execute(
            """SELECT name FROM sqlite_master
            WHERE type = 'table'
              AND name IN ('af_migrations', 'af_migration_locks')
            ORDER BY name"""
        ).fetchall()
    assert metadata == [("af_migration_locks",), ("af_migrations",)]


def test_installed_migration_cli_requires_factory_before_application_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "implicit.sqlite3"
    monkeypatch.setenv("AF_DATABASE_URL", f"sqlite+pysqlite:///{database}")

    result = runner.invoke(create_cli(), ["migrations", "status"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "configuration is invalid" in result.stderr
    assert not database.exists()


def test_installed_cli_does_not_reuse_factory_across_invocations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "ambient.sqlite3"
    monkeypatch.setenv("AF_DATABASE_URL", f"sqlite+pysqlite:///{database}")
    module = ModuleType("test_isolated_operator_profile")
    calls = 0

    def build_application():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return _application()

    module.build_application = build_application  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    cli = create_cli()

    first = runner.invoke(
        cli,
        [
            "--application-factory",
            "test_isolated_operator_profile:build_application",
            "hello",
        ],
    )
    second = runner.invoke(cli, ["migrations", "status"])

    assert first.exit_code == 0
    assert second.exit_code == 2
    assert calls == 1
    assert "configuration is invalid" in second.stderr
    assert not database.exists()


def test_installed_cli_defers_factory_import_for_nested_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    imported: list[str] = []

    def record_import(name: str) -> ModuleType:
        imported.append(name)
        return ModuleType(name)

    monkeypatch.setattr(cli_module, "import_module", record_import)

    result = runner.invoke(
        create_cli(),
        [
            "--application-factory",
            "test_deferred_operator_profile:build_application",
            "migrations",
            "--help",
        ],
    )

    assert result.exit_code == 0
    assert imported == []


def test_installed_cli_sanitizes_factory_af_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("test_secret_operator_profile")
    secret = "postgresql://user:SECRET@host/database"

    def build_application():  # type: ignore[no-untyped-def]
        raise InvalidProviderConfiguration(secret)

    module.build_application = build_application  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = runner.invoke(
        create_cli(),
        [
            "--application-factory",
            "test_secret_operator_profile:build_application",
            "hello",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "configuration is invalid" in result.stderr
    assert secret not in result.stderr


def test_programmatic_falsey_factory_does_not_fall_back() -> None:
    calls = 0

    class FalseyFactory:
        def __bool__(self) -> bool:
            return False

        def __call__(self):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            return _application()

    result = runner.invoke(create_cli(application_factory=FalseyFactory()), ["hello"])

    assert result.exit_code == 0
    assert calls == 1


@pytest.mark.parametrize(
    "factory_path",
    [
        "not a factory",
        "private_missing_module:create_application",
        "private.missing:create_application",
    ],
)
def test_installed_cli_sanitizes_invalid_application_factory(
    factory_path: str,
) -> None:
    result = runner.invoke(
        create_cli(),
        [
            "--application-factory",
            factory_path,
            "migrations",
            "status",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert factory_path not in result.stderr
    assert "configuration is invalid" in result.stderr


def test_explicit_factory_cannot_override_injected_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = _application()
    module = ModuleType("test_conflicting_operator_profile")
    module.build_application = _application  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = runner.invoke(
        create_cli(application),
        [
            "--application-factory",
            "test_conflicting_operator_profile:build_application",
            "migrations",
            "status",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "configuration is invalid" in result.stderr
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


@pytest.mark.parametrize(
    "arguments, error_type",
    [
        ({"migrations": (object(),)}, ValueError),
        ({"connection": ""}, ValueError),
        ({"owner_id": ""}, ValueError),
        ({"clock": object()}, TypeError),
        ({"lock_lease": timedelta(0)}, ValueError),
    ],
)
def test_migration_provider_rejects_invalid_configuration(
    arguments: dict[str, object],
    error_type: type[Exception],
) -> None:
    options: dict[str, object] = {
        "migrations": (_migration(),),
        "connection": "primary",
        "owner_id": "cli-test",
        "clock": lambda: _NOW,
    }
    options.update(arguments)

    with pytest.raises(error_type):
        MigrationProvider(**options)  # type: ignore[arg-type]
