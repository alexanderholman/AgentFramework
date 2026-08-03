from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

from af.application.database_setup import (
    DatabaseSetupController,
    DatabaseSetupError,
    DatabaseSetupMode,
    DatabaseSetupRequest,
    DatabaseSetupResult,
)
from af.bootstrap import create_application
from af.framework.container import ProviderRegistrar, ServiceProvider
from af.interfaces.console import create_cli

runner = CliRunner()


class FixtureDatabaseSetupService:
    def __init__(
        self,
        result: DatabaseSetupResult | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error
        self.requests: list[DatabaseSetupRequest] = []

    def setup(self, request: DatabaseSetupRequest) -> DatabaseSetupResult:
        self.requests.append(request)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class DatabaseSetupProvider(ServiceProvider):
    provider_id = "test.database-setup"

    def __init__(self, controller: DatabaseSetupController) -> None:
        self._controller = controller

    def register(self, registrar: ProviderRegistrar) -> None:
        controller = self._controller
        registrar.bind(DatabaseSetupController, lambda _: controller, replace=True)


def _result(tmp_path: Path, *, executed: bool) -> DatabaseSetupResult:
    return DatabaseSetupResult(
        mode=DatabaseSetupMode.LOCAL_DEBIAN,
        executed=executed,
        verified=executed,
        host="127.0.0.1",
        port=3306,
        database="af_test",
        user="af_test",
        env_file=str(tmp_path / ".env"),
        backup_file=str(tmp_path / ".env.bak") if executed else None,
        env_changed=executed,
        connection_url="mariadb+pymysql://af_test:***@127.0.0.1:3306/af_test",
        actions=(
            "install MariaDB packages with apt-get",
            "enable and start the mariadb service",
            "create or update the requested database and user",
            "verify the MariaDB connection through DatabaseManager",
            "atomically update the project .env file",
        ),
    )


def test_db_setup_command_previews_local_provisioning_without_mutation(tmp_path: Path) -> None:
    service = FixtureDatabaseSetupService(result=_result(tmp_path, executed=False))

    result = runner.invoke(
        create_cli(database_setup=DatabaseSetupController(service)),
        ["db", "setup", "--mode", "local-debian", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["executed"] is False
    assert payload["verified"] is False
    assert payload["database"] == "af_test"
    assert payload["connection_url"] == "mariadb+pymysql://af_test:***@127.0.0.1:3306/af_test"
    assert service.requests == [
        DatabaseSetupRequest(
            mode=DatabaseSetupMode.LOCAL_DEBIAN,
            user="af_test",
            password="af_test_pass",
            database="af_test",
            host="127.0.0.1",
            port=3306,
            project_root=tmp_path,
            execute=False,
        )
    ]


def test_db_setup_command_executes_only_with_explicit_flag_and_redacts_credentials(
    tmp_path: Path,
) -> None:
    secret = "pa:ss/word#1"
    service = FixtureDatabaseSetupService(result=_result(tmp_path, executed=True))

    result = runner.invoke(
        create_cli(database_setup=DatabaseSetupController(service)),
        [
            "db",
            "setup",
            "--mode",
            "local-debian",
            "--project-root",
            str(tmp_path),
            "--password",
            secret,
            "--execute",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["executed"] is True
    assert payload["verified"] is True
    assert secret not in result.stdout
    assert secret not in result.stderr
    assert service.requests[0].execute is True
    assert service.requests[0].password == secret


def test_db_setup_command_reports_operator_facing_setup_failure(tmp_path: Path) -> None:
    service = FixtureDatabaseSetupService(
        error=DatabaseSetupError("db setup only supports local-debian mode on Debian or Ubuntu"),
    )

    result = runner.invoke(
        create_cli(database_setup=DatabaseSetupController(service)),
        ["db", "setup", "--mode", "local-debian", "--project-root", str(tmp_path), "--execute"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "db setup only supports local-debian mode on Debian or Ubuntu\n"


def test_installed_console_loads_db_setup_controller_from_application_factory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    module = ModuleType("test_db_setup_operator_profile")
    controller = DatabaseSetupController(
        FixtureDatabaseSetupService(result=_result(tmp_path, executed=True))
    )

    def build_application():  # type: ignore[no-untyped-def]
        return create_application(providers=(DatabaseSetupProvider(controller),))

    module.build_application = build_application  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)

    result = runner.invoke(
        create_cli(),
        [
            "--application-factory",
            "test_db_setup_operator_profile:build_application",
            "db",
            "setup",
            "--mode",
            "local-debian",
            "--project-root",
            str(tmp_path),
            "--execute",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["executed"] is True
    assert payload["env_file"].endswith(".env")


def test_installed_console_help_lists_db_setup_command() -> None:
    result = runner.invoke(create_cli(), ["--help"])

    assert result.exit_code == 0
    assert "db" in result.stdout
    assert "setup" in result.stdout
