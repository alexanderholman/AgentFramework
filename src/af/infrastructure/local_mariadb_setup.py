"""Explicit supported local MariaDB setup backend."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from af.framework.database_setup import (
    DatabaseSetupError,
    DatabaseSetupRequest,
    DatabaseSetupResult,
    DatabaseSetupStatus,
)

_ACTIONS = (
    "Install MariaDB packages via apt-get",
    "Enable and start the mariadb service",
    "Create or update the local MariaDB database and user",
    "Verify the connection through AF's MariaDB manager",
    "Update AF_MARIADB_TEST_URL in the environment file",
)
_ENV_KEY = "AF_MARIADB_TEST_URL"


class CommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        description: str,
    ) -> None: ...


class SubprocessCommandRunner:
    """Trusted subprocess adapter for explicit host-level setup steps."""

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        description: str,
    ) -> None:
        try:
            subprocess.run(
                argv,
                check=True,
                text=True,
                input=input_text,
                capture_output=True,
                env=None if env is None else {**os.environ, **dict(env)},
            )
        except subprocess.CalledProcessError as error:
            raise DatabaseSetupError(f"failed to {description}") from error
        except OSError as error:
            raise DatabaseSetupError(f"failed to {description}") from error


class LocalMariaDBSetupBackend:
    def __init__(
        self,
        *,
        command_runner: CommandRunner,
        connection_verifier: Callable[[str], None],
        platform_system: Callable[[], str] = platform.system,
        os_release_loader: Callable[[], str] | None = None,
    ) -> None:
        self._command_runner = command_runner
        self._connection_verifier = connection_verifier
        self._platform_system = platform_system
        self._os_release_loader = (
            os_release_loader if os_release_loader is not None else self._load_os_release
        )

    def setup(self, request: DatabaseSetupRequest) -> DatabaseSetupResult:
        if request.host != "127.0.0.1":
            raise DatabaseSetupError("local-debian setup requires a local host")
        self._assert_supported_platform()
        env_file = request.resolved_env_file
        full_url = _connection_url(request)
        merged_content, env_changed = _merged_env_content(env_file, _ENV_KEY, full_url)
        backup_file = _backup_file(env_file) if env_changed and env_file.exists() else None
        if not request.execute:
            return DatabaseSetupResult(
                mode=request.mode,
                status=DatabaseSetupStatus.PLANNED,
                execute=False,
                project_root=str(request.project_root),
                env_file=str(env_file),
                env_backup_file=None if backup_file is None else str(backup_file),
                env_changed=env_changed,
                verification_succeeded=False,
                host=request.host,
                port=request.port,
                database=request.database,
                user=request.user,
                connection_url=_redacted_connection_url(request),
                actions=_ACTIONS,
            )

        self._run_installation()
        self._run_service_enablement()
        self._run_sql_setup(request)
        try:
            self._connection_verifier(full_url)
        except Exception as error:
            raise DatabaseSetupError("connection verification failed") from error
        if env_changed:
            if backup_file is not None:
                _write_backup(env_file, backup_file)
            _atomic_write_env(env_file, merged_content)
        return DatabaseSetupResult(
            mode=request.mode,
            status=DatabaseSetupStatus.APPLIED,
            execute=True,
            project_root=str(request.project_root),
            env_file=str(env_file),
            env_backup_file=None if backup_file is None else str(backup_file),
            env_changed=env_changed,
            verification_succeeded=True,
            host=request.host,
            port=request.port,
            database=request.database,
            user=request.user,
            connection_url=_redacted_connection_url(request),
            actions=_ACTIONS,
        )

    def _assert_supported_platform(self) -> None:
        if self._platform_system() != "Linux":
            raise DatabaseSetupError(
                "local-debian setup is only supported on Debian or Ubuntu Linux"
            )
        content = self._os_release_loader().lower()
        if "id=debian" in content or "id=ubuntu" in content:
            return
        if "id_like=" in content and ("debian" in content or "ubuntu" in content):
            return
        raise DatabaseSetupError("local-debian setup is only supported on Debian or Ubuntu Linux")

    def _run_installation(self) -> None:
        self._command_runner.run(
            ("sudo", "apt-get", "update"),
            description="refresh package metadata",
        )
        self._command_runner.run(
            (
                "sudo",
                "apt-get",
                "install",
                "--yes",
                "mariadb-server",
                "mariadb-client",
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
            description="install MariaDB packages",
        )

    def _run_service_enablement(self) -> None:
        self._command_runner.run(
            ("sudo", "systemctl", "enable", "--now", "mariadb"),
            description="enable and start the mariadb service",
        )

    def _run_sql_setup(self, request: DatabaseSetupRequest) -> None:
        self._command_runner.run(
            ("sudo", "mariadb"),
            input_text=_sql_script(request),
            description="apply local MariaDB database setup",
        )

    @staticmethod
    def _load_os_release() -> str:
        try:
            return Path("/etc/os-release").read_text(encoding="utf-8")
        except OSError as error:
            raise DatabaseSetupError("local-debian setup requires /etc/os-release") from error


def _backup_file(env_file: Path) -> Path:
    return env_file.with_name(env_file.name + ".bak")


def _quote_identifier(value: str) -> str:
    return f"`{value}`"


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_script(request: DatabaseSetupRequest) -> str:
    database = _quote_identifier(request.database)
    user = _quote_literal(request.user)
    host = _quote_literal(request.host)
    password = _quote_literal(request.password)
    return (
        "CREATE DATABASE IF NOT EXISTS "
        f"{database} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\n"
        f"CREATE USER IF NOT EXISTS {user}@{host} IDENTIFIED BY {password};\n"
        f"ALTER USER IF EXISTS {user}@{host} IDENTIFIED BY {password};\n"
        f"GRANT ALL PRIVILEGES ON {database}.* TO {user}@{host};\n"
        "FLUSH PRIVILEGES;\n"
    )


def _connection_url(request: DatabaseSetupRequest) -> str:
    return (
        "mariadb+pymysql://"
        f"{quote(request.user, safe='')}:{quote(request.password, safe='')}"
        f"@{quote(request.host, safe='')}:{request.port}/{quote(request.database, safe='')}"
    )


def _redacted_connection_url(request: DatabaseSetupRequest) -> str:
    return f"mariadb+pymysql://{request.user}:***@{request.host}:{request.port}/{request.database}"


def _merged_env_content(path: Path, key: str, value: str) -> tuple[str, bool]:
    try:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError as error:
        raise DatabaseSetupError("failed to read the environment file") from error
    lines = current.splitlines(keepends=True)
    rendered = f"{key}={value}\n"
    updated: list[str] = []
    matched = False
    changed = False
    for line in lines:
        if line.startswith(f"{key}="):
            if not matched:
                updated.append(rendered)
                changed = changed or line != rendered
                matched = True
            else:
                changed = True
            continue
        updated.append(line)
    if not matched:
        if updated and not updated[-1].endswith("\n"):
            updated[-1] = updated[-1] + "\n"
        updated.append(rendered)
        changed = True
    content = "".join(updated)
    return content, changed


def _write_backup(source: Path, backup: Path) -> None:
    try:
        shutil.copyfile(source, backup)
    except OSError as error:
        raise DatabaseSetupError("failed to back up the environment file") from error


def _atomic_write_env(path: Path, content: str) -> None:
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise DatabaseSetupError("environment file parent directory is unavailable")
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            delete=False,
        ) as handle:
            handle.write(content)
            temporary_path = handle.name
        os.replace(temporary_path, path)
    except OSError as error:
        raise DatabaseSetupError("failed to update the environment file") from error
    finally:
        if temporary_path is not None:
            Path(temporary_path).unlink(missing_ok=True)
