from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from af.application.database_setup import DatabaseSetupController
from af.framework.database_setup import (
    DatabaseSetupMode,
    DatabaseSetupRequest,
    DatabaseSetupResult,
    DatabaseSetupStatus,
)
from af.infrastructure.local_mariadb_setup import DatabaseSetupError
from af.interfaces.cli import create_cli
from af.interfaces.cli_interactions import PromptEvent, ScriptedCliInteraction

runner = CliRunner()


class _FixtureBackend:
    def __init__(self, result: DatabaseSetupResult) -> None:
        self.result = result
        self.requests: list[DatabaseSetupRequest] = []

    def setup(self, request: DatabaseSetupRequest) -> DatabaseSetupResult:
        self.requests.append(request)
        return self.result


class _FailingBackend:
    def setup(self, request: DatabaseSetupRequest) -> DatabaseSetupResult:
        del request
        raise DatabaseSetupError("local-debian provisioning is unavailable")


def _result(tmp_path: Path, *, execute: bool) -> DatabaseSetupResult:
    return DatabaseSetupResult(
        mode=DatabaseSetupMode.LOCAL_DEBIAN,
        status=DatabaseSetupStatus.APPLIED if execute else DatabaseSetupStatus.PLANNED,
        execute=execute,
        project_root=str(tmp_path),
        env_file=str(tmp_path / ".env"),
        env_backup_file=str(tmp_path / ".env.bak") if execute else None,
        env_changed=execute,
        verification_succeeded=execute,
        host="127.0.0.1",
        port=3306,
        database="af_test",
        user="af_test",
        connection_url="mariadb+pymysql://af_test:***@127.0.0.1:3306/af_test",
        actions=(
            "Install MariaDB packages via apt-get",
            "Enable and start the mariadb service",
            "Create or update the local MariaDB database and user",
            "Verify the connection through AF's MariaDB manager",
            "Update AF_MARIADB_TEST_URL in the environment file",
        ),
    )


def test_cli_exposes_db_setup_preview_command(tmp_path: Path) -> None:
    backend = _FixtureBackend(_result(tmp_path, execute=False))

    result = runner.invoke(
        create_cli(database_setup=DatabaseSetupController(backend)),
        ["db", "setup", "--mode", "local-debian", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == backend.result.to_dict()
    assert len(backend.requests) == 1
    assert backend.requests[0].execute is False
    assert backend.requests[0].mode is DatabaseSetupMode.LOCAL_DEBIAN
    assert backend.requests[0].project_root == tmp_path


def test_cli_execute_db_setup_uses_yes_without_prompt(tmp_path: Path) -> None:
    backend = _FixtureBackend(_result(tmp_path, execute=True))

    result = runner.invoke(
        create_cli(database_setup=DatabaseSetupController(backend)),
        [
            "db",
            "setup",
            "--mode",
            "local-debian",
            "--execute",
            "--yes",
            "--project-root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == backend.result.to_dict()
    assert backend.requests[0].execute is True


def test_cli_execute_db_setup_prompts_for_confirmation(tmp_path: Path) -> None:
    backend = _FixtureBackend(_result(tmp_path, execute=True))
    interactions: list[ScriptedCliInteraction] = []

    def build_interaction() -> ScriptedCliInteraction:
        interaction = ScriptedCliInteraction(confirm_responses=(True,))
        interactions.append(interaction)
        return interaction

    result = runner.invoke(
        create_cli(
            database_setup=DatabaseSetupController(backend),
            interaction_factory=build_interaction,
        ),
        ["db", "setup", "--mode", "local-debian", "--execute", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 0
    assert backend.requests[0].execute is True
    assert tuple(interactions[0].events) == (
        PromptEvent(
            kind="confirm",
            message="Apply local-debian MariaDB provisioning changes?",
        ),
    )


def test_cli_db_setup_fails_closed_without_confirmation_adapter(tmp_path: Path) -> None:
    backend = _FixtureBackend(_result(tmp_path, execute=True))

    result = runner.invoke(
        create_cli(database_setup=DatabaseSetupController(backend)),
        ["db", "setup", "--mode", "local-debian", "--execute", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "interactive input is unavailable for this CLI invocation\n"
    assert backend.requests == []


def test_cli_reports_database_setup_failure(tmp_path: Path) -> None:
    result = runner.invoke(
        create_cli(database_setup=DatabaseSetupController(_FailingBackend())),
        ["db", "setup", "--mode", "local-debian", "--project-root", str(tmp_path)],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "local-debian provisioning is unavailable\n"
