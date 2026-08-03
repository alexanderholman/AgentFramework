"""Infrastructure adapters for safe local MariaDB setup."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from af.application.database_setup import (
    ConnectionUrlWriter,
    DatabaseConnectionVerifier,
    DatabaseSetupError,
    DatabaseSetupMode,
    DatabaseSetupPlan,
    DatabaseSetupRequest,
    EnvironmentFileUpdate,
    LocalDatabaseProvisioner,
)
from af.framework.database_manager import DatabaseManager
from af.framework.relational import DatabaseStatement
from af.infrastructure.mariadb_manager import mariadb_registration

__all__ = ("AtomicEnvFileWriter", "LocalMariaDBProvisioner", "MariaDBConnectionVerifier")

_ENV_ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=.*$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")


class LocalMariaDBProvisioner(LocalDatabaseProvisioner):
    def __init__(
        self,
        *,
        runner: Callable[..., None] | None = None,
        os_release: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        self._runner = _default_runner if runner is None else runner
        self._os_release = _read_os_release if os_release is None else os_release

    def preview(self, request: DatabaseSetupRequest) -> DatabaseSetupPlan:
        _validate_local_debian_request(request, self._os_release())
        return DatabaseSetupPlan(
            actions=(
                "install MariaDB packages with apt-get",
                "enable and start the mariadb service",
                "create or update the requested database and user",
            )
        )

    def apply(self, request: DatabaseSetupRequest) -> DatabaseSetupPlan:
        plan = self.preview(request)
        self._run(
            ("sudo", "apt-get", "install", "-y", "mariadb-server", "mariadb-client"),
            env={"DEBIAN_FRONTEND": "noninteractive"},
            failure="could not install MariaDB packages",
        )
        self._run(
            ("sudo", "systemctl", "enable", "--now", "mariadb"),
            failure="could not enable and start the MariaDB service",
        )
        self._run(
            ("sudo", "mariadb"),
            stdin=_sql_for_request(request).encode("utf-8"),
            failure="could not create or update the MariaDB database and user",
        )
        return plan

    def _run(
        self,
        command: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
        failure: str,
    ) -> None:
        try:
            self._runner(command, env=env, stdin=stdin)
        except DatabaseSetupError:
            raise
        except Exception as error:
            raise DatabaseSetupError(failure) from error


class AtomicEnvFileWriter(ConnectionUrlWriter):
    def update(self, *, env_file: Path, key: str, value: str) -> EnvironmentFileUpdate:
        if type(key) is not str or not key:
            raise DatabaseSetupError("environment key is invalid")
        if type(value) is not str or not value:
            raise DatabaseSetupError("environment value is invalid")
        parent = env_file.parent
        if not parent.exists() or not parent.is_dir():
            raise DatabaseSetupError("environment file directory is unavailable")
        if env_file.exists() and not env_file.is_file():
            raise DatabaseSetupError("could not update environment file")
        try:
            original = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        except OSError as error:
            raise DatabaseSetupError("could not update environment file") from error
        updated = _merge_env_value(original, key=key, value=value)
        if updated == original:
            return EnvironmentFileUpdate(
                env_file=str(env_file),
                backup_file=None,
                changed=False,
            )

        backup_file = env_file.with_name(env_file.name + ".bak") if env_file.exists() else None
        temp_path: Path | None = None
        try:
            if backup_file is not None:
                _write_text_with_mode(backup_file, original, mode=_file_mode(env_file))
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=parent,
                prefix=env_file.name + ".",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(updated)
                temp_path = Path(handle.name)
            temp_path.replace(env_file)
        except OSError as error:
            if temp_path is not None:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)
            raise DatabaseSetupError("could not update environment file") from error

        return EnvironmentFileUpdate(
            env_file=str(env_file),
            backup_file=str(backup_file) if backup_file is not None else None,
            changed=True,
        )


class MariaDBConnectionVerifier(DatabaseConnectionVerifier):
    def verify(self, *, url: str) -> str:
        manager = cast(
            Any,
            DatabaseManager(
                connections={"primary": {"driver": "mariadb", "url": url}},
                default="primary",
                read="primary",
                write="primary",
                deployment_profile="local",
                policy_version="db-setup-v1",
            ),
        )
        try:
            manager.install(mariadb_registration())
            with manager.transaction(read_only=True) as active:
                rows = active.query(
                    DatabaseStatement("SELECT VERSION() AS version", read_only=True)
                )
            version = rows[0]["version"]
            if type(version) is not str or not version:
                raise DatabaseSetupError("MariaDB verification returned an invalid server version")
            return version
        except DatabaseSetupError:
            raise
        except Exception as error:
            raise DatabaseSetupError("could not verify the MariaDB connection") from error
        finally:
            manager.close()


def _validate_local_debian_request(
    request: DatabaseSetupRequest,
    os_release: dict[str, str],
) -> None:
    if request.mode is not DatabaseSetupMode.LOCAL_DEBIAN:
        raise DatabaseSetupError("db setup only supports local-debian mode")
    distro = os_release.get("ID", "").lower()
    like = os_release.get("ID_LIKE", "").lower().split()
    if distro not in {"debian", "ubuntu"} and not {"debian", "ubuntu"} & set(like):
        raise DatabaseSetupError("db setup only supports local-debian mode on Debian or Ubuntu")
    if request.host not in {"127.0.0.1", "localhost"}:
        raise DatabaseSetupError("local-debian mode must use a local host")


def _sql_for_request(request: DatabaseSetupRequest) -> str:
    return "\n".join(
        (
            (
                "CREATE DATABASE IF NOT EXISTS "
                f"{_quote_identifier(request.database)} "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
            ),
            (
                "CREATE USER IF NOT EXISTS "
                f"{_quote_literal(request.user)}@{_quote_literal(request.host)} "
                f"IDENTIFIED BY {_quote_literal(request.password)};"
            ),
            (
                "ALTER USER "
                f"{_quote_literal(request.user)}@{_quote_literal(request.host)} "
                f"IDENTIFIED BY {_quote_literal(request.password)};"
            ),
            (
                "GRANT ALL PRIVILEGES ON "
                f"{_quote_identifier(request.database)}.* TO "
                f"{_quote_literal(request.user)}@{_quote_literal(request.host)};"
            ),
            "FLUSH PRIVILEGES;",
        )
    )


def _quote_identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise DatabaseSetupError(
            "database identifiers must contain only letters, numbers, and underscores"
        )
    return f"`{value}`"


def _quote_literal(value: str) -> str:
    if type(value) is not str:
        raise DatabaseSetupError("database SQL values must be text")
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    return "'" + escaped + "'"


def _file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _write_text_with_mode(path: Path, content: str, *, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _merge_env_value(content: str, *, key: str, value: str) -> str:
    lines = content.splitlines(keepends=True)
    matches: list[int] = []
    for index, line in enumerate(lines):
        matched = _ENV_ASSIGNMENT.match(line)
        if matched is not None and matched.group(1) == key:
            matches.append(index)
    if len(matches) > 1:
        raise DatabaseSetupError(f"duplicate {key} entries prevent a safe environment update")
    replacement = f"{key}={value}\n"
    if len(matches) == 1:
        lines[matches[0]] = replacement
    else:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] = lines[-1] + "\n"
        lines.append(replacement)
    return "".join(lines)


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        content = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError as error:
        raise DatabaseSetupError("db setup requires /etc/os-release on Debian or Ubuntu") from error
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        data[key] = raw_value.strip().strip('"')
    return data


def _default_runner(
    command: tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
) -> None:
    resolved_env = os.environ.copy()
    if env is not None:
        resolved_env.update(env)
    subprocess.run(
        command,
        check=True,
        env=resolved_env,
        input=stdin,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
