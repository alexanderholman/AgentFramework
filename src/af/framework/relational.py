"""Driver-neutral relational database contracts."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Protocol

from af.framework.errors import PlatformError, PlatformReason

__all__ = (
    "DatabaseCursor",
    "DatabaseDriver",
    "DatabaseFeature",
    "DatabaseOperationError",
    "DatabaseRow",
    "DatabaseStatement",
    "DatabaseTransaction",
    "DatabaseValue",
    "UnsupportedDatabaseFeature",
)

type DatabaseValue = bool | bytes | date | datetime | float | int | str | None

_PARAMETER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_SQL_LENGTH = 65_536
_MAX_PARAMETERS = 256
_MAX_VALUE_LENGTH = 1_048_576


class DatabaseFeature(StrEnum):
    """Capabilities that must be verified before use."""

    TRANSACTIONS = "transactions"
    TRANSACTIONAL_DDL = "transactional-ddl"
    SAVEPOINTS = "savepoints"
    STREAMING = "streaming"
    ADVISORY_LOCKS = "advisory-locks"
    JSON = "json"
    VECTOR = "vector"
    FULL_TEXT_SEARCH = "full-text-search"
    POOLING = "pooling"
    CANCELLATION = "cancellation"


class DatabaseOperationError(PlatformError):
    """Sanitized relational operation failure."""

    def __init__(
        self,
        *,
        reason: PlatformReason = PlatformReason.PERMANENT_FAILURE,
        stage: str = "database.operation",
        protected_cause: BaseException | None = None,
    ) -> None:
        super().__init__(reason, stage=stage, protected_cause=protected_cause)


class UnsupportedDatabaseFeature(DatabaseOperationError):
    """A requested feature is not verified for the selected driver."""

    def __init__(self, feature: DatabaseFeature) -> None:
        self.feature = feature
        super().__init__(
            reason=PlatformReason.CAPABILITY_UNAVAILABLE,
            stage="database.capability",
        )


def _value(value: object) -> DatabaseValue:
    if value is None:
        return None
    if type(value) is bool:
        return bool(value)
    if type(value) is int:
        return int(value)
    if type(value) is date:
        return value
    if type(value) is datetime:
        return value
    if type(value) is float and isfinite(value):
        return value
    if type(value) is str and len(value) <= _MAX_VALUE_LENGTH:
        return value
    if type(value) is bytes and len(value) <= _MAX_VALUE_LENGTH:
        return value
    raise ValueError("database parameter value is invalid or too large")


@dataclass(frozen=True, slots=True, init=False, repr=False)
class DatabaseStatement:
    """One bounded statement with an immutable parameter snapshot."""

    sql: str
    parameters: Mapping[str, DatabaseValue]
    read_only: bool

    def __init__(
        self,
        sql: str,
        parameters: Mapping[str, object] | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        if (
            type(sql) is not str
            or not sql.strip()
            or len(sql) > _MAX_SQL_LENGTH
            or re.search(r"[\x00\u2028\u2029]", sql)
        ):
            raise ValueError("database SQL must be bounded non-empty text")
        if type(read_only) is not bool:
            raise ValueError("database statement read-only flag must be boolean")
        raw = {} if parameters is None else parameters
        if not isinstance(raw, Mapping) or len(raw) > _MAX_PARAMETERS:
            raise ValueError("database statement parameters are invalid")
        snapshot: dict[str, DatabaseValue] = {}
        for key, value in raw.items():
            if type(key) is not str or _PARAMETER.fullmatch(key) is None:
                raise ValueError("database parameter names must be canonical")
            snapshot[key] = _value(value)
        object.__setattr__(self, "sql", sql)
        object.__setattr__(self, "parameters", MappingProxyType(snapshot))
        object.__setattr__(self, "read_only", read_only)

    def __repr__(self) -> str:
        return (
            f"DatabaseStatement(read_only={self.read_only!r}, "
            "sql='[REDACTED]', parameters='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True, repr=False)
class DatabaseRow:
    """Immutable ordered row with deterministic name lookup."""

    columns: tuple[str, ...]
    values: tuple[DatabaseValue, ...]

    def __post_init__(self) -> None:
        if (
            type(self.columns) is not tuple
            or type(self.values) is not tuple
            or len(self.columns) != len(self.values)
            or len(set(self.columns)) != len(self.columns)
            or any(type(column) is not str or not column for column in self.columns)
        ):
            raise ValueError("database row shape is invalid")
        for value in self.values:
            _value(value)

    def __getitem__(self, column: str) -> DatabaseValue:
        if type(column) is not str:
            raise TypeError("database row columns must be strings")
        try:
            return self.values[self.columns.index(column)]
        except ValueError as error:
            raise KeyError(column) from error

    def as_mapping(self) -> dict[str, DatabaseValue]:
        return dict(zip(self.columns, self.values, strict=True))

    def __repr__(self) -> str:
        return f"DatabaseRow(columns={self.columns!r}, values='[REDACTED]')"


class DatabaseCursor(Iterator[DatabaseRow], Protocol):
    """A cursor whose lifetime is bounded by its transaction."""

    def close(self) -> None: ...


class DatabaseTransaction(Protocol):
    """Synchronous transaction contract shared by relational drivers."""

    def execute(self, statement: DatabaseStatement, /) -> int: ...

    def query(self, statement: DatabaseStatement, /) -> tuple[DatabaseRow, ...]: ...

    def stream(self, statement: DatabaseStatement, /) -> DatabaseCursor: ...

    def savepoint(self, name: str, /) -> AbstractContextManager[None]: ...


class DatabaseDriver(Protocol):
    """Named-driver boundary consumed by DatabaseManager."""

    @property
    def features(self) -> frozenset[DatabaseFeature]: ...

    def supports(self, feature: DatabaseFeature, /) -> bool: ...

    def require(self, feature: DatabaseFeature, /) -> None: ...

    def transaction(
        self,
        *,
        read_only: bool = False,
    ) -> AbstractContextManager[DatabaseTransaction]: ...

    def cancel(self) -> None: ...

    def close(self) -> None: ...
