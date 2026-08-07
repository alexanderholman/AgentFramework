"""Immutable contracts for explicit local MariaDB setup operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from af.framework.errors import AFError

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_PASSWORD_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_HOST_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class DatabaseSetupError(AFError):
    """Sanitized failure for explicit database setup operations."""


class DatabaseSetupMode(StrEnum):
    LOCAL_DEBIAN = "local-debian"


class DatabaseSetupStatus(StrEnum):
    PLANNED = "planned"
    APPLIED = "applied"


def _identifier(value: object, *, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _bounded_password(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or _PASSWORD_CONTROL.search(value) is not None
    ):
        raise ValueError("database password is invalid")
    return value


def _host(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 255
        or _HOST_CONTROL.search(value) is not None
    ):
        raise ValueError("database host is invalid")
    return "127.0.0.1" if value == "localhost" else value


@dataclass(frozen=True, slots=True)
class DatabaseSetupRequest:
    project_root: Path
    env_file: Path | None = None
    mode: DatabaseSetupMode = DatabaseSetupMode.LOCAL_DEBIAN
    execute: bool = False
    user: str = "af_test"
    password: str = "af_test_pass"
    database: str = "af_test"
    host: str = "127.0.0.1"
    port: int = 3306

    def __post_init__(self) -> None:
        if not isinstance(self.project_root, Path):
            raise TypeError("project_root must be a Path")
        if self.env_file is not None and not isinstance(self.env_file, Path):
            raise TypeError("env_file must be a Path")
        object.__setattr__(self, "mode", DatabaseSetupMode(self.mode))
        if type(self.execute) is not bool:
            raise ValueError("database setup execute flag must be boolean")
        object.__setattr__(self, "user", _identifier(self.user, label="database user"))
        object.__setattr__(self, "database", _identifier(self.database, label="database name"))
        object.__setattr__(self, "password", _bounded_password(self.password))
        object.__setattr__(self, "host", _host(self.host))
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("database port is invalid")

    @property
    def resolved_env_file(self) -> Path:
        return self.env_file if self.env_file is not None else self.project_root / ".env"


@dataclass(frozen=True, slots=True)
class DatabaseSetupResult:
    mode: DatabaseSetupMode
    status: DatabaseSetupStatus
    execute: bool
    project_root: str
    env_file: str
    env_backup_file: str | None
    env_changed: bool
    verification_succeeded: bool
    host: str
    port: int
    database: str
    user: str
    connection_url: str
    actions: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", DatabaseSetupMode(self.mode))
        object.__setattr__(self, "status", DatabaseSetupStatus(self.status))
        if type(self.execute) is not bool:
            raise ValueError("database setup execute flag must be boolean")
        if type(self.project_root) is not str or not self.project_root:
            raise ValueError("project root is invalid")
        if type(self.env_file) is not str or not self.env_file:
            raise ValueError("environment file is invalid")
        if self.env_backup_file is not None and (
            type(self.env_backup_file) is not str or not self.env_backup_file
        ):
            raise ValueError("environment backup file is invalid")
        if type(self.env_changed) is not bool:
            raise ValueError("environment change state is invalid")
        if type(self.verification_succeeded) is not bool:
            raise ValueError("verification state is invalid")
        object.__setattr__(self, "database", _identifier(self.database, label="database name"))
        object.__setattr__(self, "user", _identifier(self.user, label="database user"))
        object.__setattr__(self, "host", _host(self.host))
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("database port is invalid")
        if (
            type(self.connection_url) is not str
            or not self.connection_url
            or len(self.connection_url) > 4096
        ):
            raise ValueError("connection URL is invalid")
        if (
            type(self.actions) is not tuple
            or not self.actions
            or any(type(action) is not str or not action for action in self.actions)
        ):
            raise ValueError("actions are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "actions": list(self.actions),
            "connection_url": self.connection_url,
            "database": self.database,
            "env_backup_file": self.env_backup_file,
            "env_changed": self.env_changed,
            "env_file": self.env_file,
            "execute": self.execute,
            "host": self.host,
            "mode": self.mode.value,
            "port": self.port,
            "project_root": self.project_root,
            "status": self.status.value,
            "user": self.user,
            "verification_succeeded": self.verification_succeeded,
        }


class DatabaseSetupBackend(Protocol):
    def setup(self, request: DatabaseSetupRequest) -> DatabaseSetupResult: ...
