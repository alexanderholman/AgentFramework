"""Application boundary for safe local MariaDB setup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from af.framework.errors import AFError

__all__ = (
    "ConnectionUrlWriter",
    "ConnectionUrlWriterToken",
    "DatabaseConnectionVerifier",
    "DatabaseConnectionVerifierToken",
    "DatabaseSetupController",
    "DatabaseSetupError",
    "DatabaseSetupMode",
    "DatabaseSetupPlan",
    "DatabaseSetupRequest",
    "DatabaseSetupResult",
    "DatabaseSetupService",
    "EnvironmentFileUpdate",
    "LocalDatabaseProvisioner",
    "LocalDatabaseProvisionerToken",
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
_EXECUTION_ACTIONS = (
    "verify the MariaDB connection through DatabaseManager",
    "atomically update the project .env file",
)


class DatabaseSetupError(AFError, ValueError):
    """Raised when operator-supplied database setup input or execution is unsafe."""


class DatabaseSetupMode(StrEnum):
    LOCAL_DEBIAN = "local-debian"


@dataclass(frozen=True, slots=True)
class DatabaseSetupRequest:
    mode: DatabaseSetupMode
    user: str
    password: str
    database: str
    host: str
    port: int
    project_root: Path
    execute: bool


@dataclass(frozen=True, slots=True)
class DatabaseSetupPlan:
    actions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.actions or any(type(item) is not str or not item for item in self.actions):
            raise ValueError("database setup actions must be bounded non-empty text")


@dataclass(frozen=True, slots=True)
class EnvironmentFileUpdate:
    env_file: str
    backup_file: str | None
    changed: bool


class LocalDatabaseProvisioner(Protocol):
    def preview(self, request: DatabaseSetupRequest) -> DatabaseSetupPlan: ...

    def apply(self, request: DatabaseSetupRequest) -> DatabaseSetupPlan: ...


class DatabaseConnectionVerifier(Protocol):
    def verify(self, *, url: str) -> str: ...


class ConnectionUrlWriter(Protocol):
    def update(self, *, env_file: Path, key: str, value: str) -> EnvironmentFileUpdate: ...


class LocalDatabaseProvisionerToken:
    """Concrete DI token for the local database provisioner."""


class DatabaseConnectionVerifierToken:
    """Concrete DI token for connection verification."""


class ConnectionUrlWriterToken:
    """Concrete DI token for env-file updates."""


@dataclass(frozen=True, slots=True)
class DatabaseSetupResult:
    mode: DatabaseSetupMode
    executed: bool
    verified: bool
    host: str
    port: int
    database: str
    user: str
    env_file: str
    backup_file: str | None
    env_changed: bool
    connection_url: str
    actions: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "actions": list(self.actions),
            "backup_file": self.backup_file,
            "connection_url": self.connection_url,
            "database": self.database,
            "env_changed": self.env_changed,
            "env_file": self.env_file,
            "executed": self.executed,
            "host": self.host,
            "mode": self.mode.value,
            "port": self.port,
            "user": self.user,
            "verified": self.verified,
        }


class DatabaseSetupService:
    def __init__(
        self,
        provisioner: LocalDatabaseProvisioner,
        verifier: DatabaseConnectionVerifier,
        writer: ConnectionUrlWriter,
    ) -> None:
        self._provisioner = provisioner
        self._verifier = verifier
        self._writer = writer

    def setup(self, request: DatabaseSetupRequest) -> DatabaseSetupResult:
        normalized = _validate_request(request)
        env_file = normalized.project_root / ".env"
        redacted_url = _redacted_connection_url(normalized)
        if not normalized.execute:
            plan = self._provisioner.preview(normalized)
            return DatabaseSetupResult(
                mode=normalized.mode,
                executed=False,
                verified=False,
                host=normalized.host,
                port=normalized.port,
                database=normalized.database,
                user=normalized.user,
                env_file=str(env_file),
                backup_file=None,
                env_changed=False,
                connection_url=redacted_url,
                actions=plan.actions,
            )

        plan = self._provisioner.apply(normalized)
        encoded_url = _encoded_connection_url(normalized)
        self._verifier.verify(url=encoded_url)
        update = self._writer.update(
            env_file=env_file,
            key="AF_MARIADB_TEST_URL",
            value=encoded_url,
        )
        return DatabaseSetupResult(
            mode=normalized.mode,
            executed=True,
            verified=True,
            host=normalized.host,
            port=normalized.port,
            database=normalized.database,
            user=normalized.user,
            env_file=update.env_file,
            backup_file=update.backup_file,
            env_changed=update.changed,
            connection_url=redacted_url,
            actions=plan.actions + _EXECUTION_ACTIONS,
        )


class DatabaseSetupController:
    def __init__(self, service: object) -> None:
        self._service = service

    def setup(self, request: DatabaseSetupRequest) -> DatabaseSetupResult:
        setup = getattr(self._service, "setup", None)
        if not callable(setup):
            raise TypeError("database setup service is invalid")
        result = setup(request)
        if not isinstance(result, DatabaseSetupResult):
            raise TypeError("database setup service returned an invalid result")
        return result


def _validate_request(request: DatabaseSetupRequest) -> DatabaseSetupRequest:
    if not isinstance(request, DatabaseSetupRequest):
        raise TypeError("database setup request is invalid")
    if request.mode is not DatabaseSetupMode.LOCAL_DEBIAN:
        raise DatabaseSetupError("db setup only supports local-debian mode")
    if _IDENTIFIER.fullmatch(request.user) is None:
        raise DatabaseSetupError(
            "database user must contain only letters, numbers, and underscores"
        )
    if _IDENTIFIER.fullmatch(request.database) is None:
        raise DatabaseSetupError(
            "database name must contain only letters, numbers, and underscores"
        )
    if type(request.password) is not str or not request.password.strip():
        raise DatabaseSetupError("database password must not be blank")
    if type(request.host) is not str or request.host not in _LOCAL_HOSTS:
        raise DatabaseSetupError("local-debian mode must use a local host")
    if type(request.port) is not int or not 1 <= request.port <= 65_535:
        raise DatabaseSetupError("database port must be a valid TCP port")
    if not isinstance(request.project_root, Path) or not request.project_root.is_dir():
        raise DatabaseSetupError("project root must be an existing directory")
    if type(request.execute) is not bool:
        raise DatabaseSetupError("database setup execute flag is invalid")
    return request


def _encoded_connection_url(request: DatabaseSetupRequest) -> str:
    return (
        "mariadb+pymysql://"
        f"{quote(request.user, safe='')}:{quote(request.password, safe='')}"
        f"@{request.host}:{request.port}/{quote(request.database, safe='')}"
    )


def _redacted_connection_url(request: DatabaseSetupRequest) -> str:
    return f"mariadb+pymysql://{request.user}:***@{request.host}:{request.port}/{request.database}"
