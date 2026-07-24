"""Repository-owned query construction with sealed policy predicates."""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType

from af.framework.errors import PlatformReason
from af.framework.relational import (
    DatabaseOperationError,
    DatabaseStatement,
    DatabaseValue,
)

__all__ = ("SealedQueryPolicy",)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_MAX_POLICY_PREDICATES = 16
_MAX_CALLER_PREDICATES = 32
_MAX_COLUMNS = 64
_MAX_LIMIT = 10_000
_MAX_OFFSET = 1_000_000


def _identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical SQL identifier")
    return value


def _snapshot(
    values: Mapping[str, object],
    *,
    label: str,
    maximum: int,
) -> Mapping[str, DatabaseValue]:
    if not isinstance(values, Mapping) or not values or len(values) > maximum:
        raise ValueError(f"{label} must be a bounded non-empty mapping")
    for key in values:
        _identifier(key, f"{label} column")
    validated = DatabaseStatement(
        "SELECT 1",
        values,
        read_only=True,
    ).parameters
    return MappingProxyType(dict(validated))


class SealedQueryPolicy:
    """Build SELECT statements whose mandatory predicates callers cannot alter."""

    def __init__(self, predicates: Mapping[str, object]) -> None:
        self._predicates = _snapshot(
            predicates,
            label="database policy predicates",
            maximum=_MAX_POLICY_PREDICATES,
        )

    def select(
        self,
        *,
        table: str,
        columns: tuple[str, ...],
        where: Mapping[str, object] | None = None,
        order_by: tuple[str, ...] = (),
        limit: int = 100,
        offset: int = 0,
    ) -> DatabaseStatement:
        """Compile a bounded equality query around repository-owned policy."""

        table = _identifier(table, "database query table")
        if (
            type(columns) is not tuple
            or not columns
            or len(columns) > _MAX_COLUMNS
            or len(set(columns)) != len(columns)
        ):
            raise ValueError("database query columns must be bounded, non-empty, and unique")
        selected = tuple(_identifier(column, "database query column") for column in columns)
        if (
            type(order_by) is not tuple
            or len(set(order_by)) != len(order_by)
            or any(
                _identifier(column, "database ordering column") not in selected
                for column in order_by
            )
        ):
            raise ValueError("database ordering must use unique selected columns")
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise ValueError("database query limit is invalid")
        if type(offset) is not int or not 0 <= offset <= _MAX_OFFSET:
            raise ValueError("database query offset is invalid")

        caller = (
            {}
            if where is None
            else dict(
                _snapshot(
                    where,
                    label="database caller predicates",
                    maximum=_MAX_CALLER_PREDICATES,
                )
            )
        )
        for column, value in self._predicates.items():
            if column in caller and caller[column] != value:
                raise DatabaseOperationError(
                    reason=PlatformReason.AUTHORIZATION_DENIED,
                    stage="database.policy",
                )
            caller.pop(column, None)

        parameters: dict[str, object] = {}
        clauses: list[str] = []
        for index, (column, value) in enumerate(self._predicates.items()):
            parameter = f"af_policy_{index}"
            clauses.append(f'"{column}" = :{parameter}')
            parameters[parameter] = value
        for index, (column, value) in enumerate(sorted(caller.items())):
            parameter = f"af_filter_{index}"
            clauses.append(f'"{column}" = :{parameter}')
            parameters[parameter] = value
        parameters["af_limit"] = limit
        parameters["af_offset"] = offset

        sql = (
            f"SELECT {', '.join(f'''\"{column}\"''' for column in selected)} "
            f'FROM "{table}" WHERE {" AND ".join(clauses)}'
        )
        if order_by:
            sql += " ORDER BY " + ", ".join(f'"{column}"' for column in order_by)
        sql += " LIMIT :af_limit OFFSET :af_offset"
        return DatabaseStatement(sql, parameters, read_only=True)

    def __repr__(self) -> str:
        return "SealedQueryPolicy(predicates='[REDACTED]')"
