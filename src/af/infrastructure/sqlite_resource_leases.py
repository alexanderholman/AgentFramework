"""Durable SQLite resource-lease repository with atomic aggregate admission."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import cast

from af.domain.resources import (
    FenceToken,
    Lease,
    LeaseAdmissionResult,
    LeaseAdmissionStatus,
    LeaseRepository,
    LeaseState,
    ResourceBudget,
    ResourceIdentity,
    ResourceKind,
)
from af.framework.errors import PlatformReason
from af.framework.relational import DatabaseOperationError, DatabaseRow, DatabaseStatement
from af.infrastructure.sqlite import SQLiteDriver

__all__ = ("SQLiteLeaseRepository",)

_TABLE = "af_resource_leases"
_CONSUMING_STATES = (LeaseState.RESERVED.value, LeaseState.ACTIVE.value)


def _timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("resource lease timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"stored resource lease {field} is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError(f"stored resource lease {field} is invalid")
    return parsed.astimezone(UTC)


def _optional_timestamp(value: object, field: str) -> datetime | None:
    return None if value is None else _parse_timestamp(value, field)


def _required_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if type(value) is not str:
        raise ValueError(f"stored resource lease {field} is invalid")
    return value


def _optional_text(payload: dict[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is not None and type(value) is not str:
        raise ValueError(f"stored resource lease {field} is invalid")
    return value


def _required_int(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int:
        raise ValueError(f"stored resource lease {field} is invalid")
    return value


def _optional_int(payload: dict[str, object], field: str) -> int | None:
    value = payload.get(field)
    if value is not None and type(value) is not int:
        raise ValueError(f"stored resource lease {field} is invalid")
    return value


def _required_float(payload: dict[str, object], field: str) -> float:
    value = payload.get(field)
    if type(value) not in {int, float}:
        raise ValueError(f"stored resource lease {field} is invalid")
    parsed = float(cast(int | float, value))
    if not isfinite(parsed):
        raise ValueError(f"stored resource lease {field} is invalid")
    return parsed


def _optional_float(payload: dict[str, object], field: str) -> float | None:
    value = payload.get(field)
    return None if value is None else _required_float({field: value}, field)


def _mapping(payload: dict[str, object], field: str) -> dict[str, object]:
    value = payload.get(field)
    if type(value) is not dict:
        raise ValueError(f"stored resource lease {field} is invalid")
    return cast(dict[str, object], value)


def _optional_string_map(payload: dict[str, object], field: str) -> dict[str, str] | None:
    value = payload.get(field)
    if value is None:
        return None
    if type(value) is not dict or any(
        type(key) is not str or type(item) is not str for key, item in value.items()
    ):
        raise ValueError(f"stored resource lease {field} is invalid")
    return cast(dict[str, str], value)


def _identity_payload(identity: ResourceIdentity) -> dict[str, object]:
    return {
        "host_id": identity.host_id,
        "instance_id": identity.instance_id,
        "kind": identity.kind.value,
        "topology": identity.topology,
    }


def _identity_text(identity: ResourceIdentity) -> str:
    return json.dumps(
        _identity_payload(identity),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _lease_text(lease: Lease) -> str:
    fence = (
        None
        if lease.fence_token is None
        else {
            "created_at": _timestamp(lease.fence_token.created_at),
            "value": lease.fence_token.value,
        }
    )
    payload: dict[str, object] = {
        "activated_at": None if lease.activated_at is None else _timestamp(lease.activated_at),
        "authorization_digest": lease.authorization_digest,
        "authorized_by": lease.authorized_by,
        "budget": {
            "identity": _identity_payload(lease.budget.identity),
            "max_concurrent": lease.budget.max_concurrent,
            "max_duration_seconds": (
                None
                if lease.budget.max_duration is None
                else lease.budget.max_duration.total_seconds()
            ),
            "max_quantity": lease.budget.max_quantity,
        },
        "capability_digest": lease.capability_digest,
        "created_at": _timestamp(lease.created_at),
        "expires_at": None if lease.expires_at is None else _timestamp(lease.expires_at),
        "fence_token": fence,
        "id": lease.id,
        "invocation_id": lease.invocation_id,
        "last_renewed_at": (
            None if lease.last_renewed_at is None else _timestamp(lease.last_renewed_at)
        ),
        "quantity": lease.quantity,
        "released_at": None if lease.released_at is None else _timestamp(lease.released_at),
        "renewed_count": lease.renewed_count,
        "requested_at": _timestamp(lease.requested_at),
        "runtime_digest": lease.runtime_digest,
        "scope": lease.scope,
        "state": lease.state.value,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _lease_from_text(value: object) -> Lease:
    if type(value) is not str:
        raise ValueError("stored resource lease payload is invalid")
    raw: object = json.loads(value)
    if type(raw) is not dict:
        raise ValueError("stored resource lease payload is invalid")
    payload = cast(dict[str, object], raw)
    budget_payload = _mapping(payload, "budget")
    identity_payload = _mapping(budget_payload, "identity")
    identity = ResourceIdentity(
        kind=ResourceKind(_required_text(identity_payload, "kind")),
        host_id=_required_text(identity_payload, "host_id"),
        instance_id=_required_text(identity_payload, "instance_id"),
        topology=_optional_string_map(identity_payload, "topology"),
    )
    duration_seconds = _optional_float(budget_payload, "max_duration_seconds")
    budget = ResourceBudget(
        identity=identity,
        max_quantity=_required_float(budget_payload, "max_quantity"),
        max_duration=None if duration_seconds is None else timedelta(seconds=duration_seconds),
        max_concurrent=_optional_int(budget_payload, "max_concurrent"),
    )
    fence_payload = payload.get("fence_token")
    if fence_payload is None:
        fence = None
    elif type(fence_payload) is dict:
        resolved_fence = cast(dict[str, object], fence_payload)
        fence = FenceToken(
            value=_required_int(resolved_fence, "value"),
            created_at=_parse_timestamp(resolved_fence.get("created_at"), "fence creation"),
        )
    else:
        raise ValueError("stored resource lease fence is invalid")
    return Lease(
        id=_required_text(payload, "id"),
        budget=budget,
        state=LeaseState(_required_text(payload, "state")),
        quantity=_required_float(payload, "quantity"),
        requested_at=_parse_timestamp(payload.get("requested_at"), "request time"),
        created_at=_parse_timestamp(payload.get("created_at"), "creation time"),
        activated_at=_optional_timestamp(payload.get("activated_at"), "activation time"),
        expires_at=_optional_timestamp(payload.get("expires_at"), "expiry time"),
        released_at=_optional_timestamp(payload.get("released_at"), "release time"),
        fence_token=fence,
        invocation_id=_optional_text(payload, "invocation_id"),
        capability_digest=_optional_text(payload, "capability_digest"),
        runtime_digest=_optional_text(payload, "runtime_digest"),
        authorized_by=_optional_text(payload, "authorized_by"),
        authorization_digest=_optional_text(payload, "authorization_digest"),
        renewed_count=_required_int(payload, "renewed_count"),
        last_renewed_at=_optional_timestamp(payload.get("last_renewed_at"), "renewal time"),
        scope=_optional_string_map(payload, "scope"),
    )


def _stored_float(value: object, field: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"stored resource lease {field} is invalid")
    parsed = float(cast(int | float, value))
    if not isfinite(parsed):
        raise ValueError(f"stored resource lease {field} is invalid")
    return parsed


def _stored_optional_int(value: object, field: str) -> int | None:
    if value is not None and type(value) is not int:
        raise ValueError(f"stored resource lease {field} is invalid")
    return value


def _integrity(error: BaseException) -> DatabaseOperationError:
    return DatabaseOperationError(
        reason=PlatformReason.INTEGRITY_FAILURE,
        stage="resource-lease.repository",
        protected_cause=error,
    )


class SQLiteLeaseRepository(LeaseRepository):
    """Persist leases through SQLite's serialized writer transaction boundary."""

    def __init__(self, database: SQLiteDriver) -> None:
        if not isinstance(database, SQLiteDriver):
            raise TypeError("SQLite lease repository requires a SQLiteDriver")
        self._database = database
        self._install()

    def save(self, lease: Lease) -> None:
        parameters = self._parameters(lease)
        with self._database.transaction() as transaction:
            exists = transaction.query(
                DatabaseStatement(
                    f"SELECT lease_id FROM {_TABLE} WHERE lease_id = :lease_id",
                    {"lease_id": lease.id},
                    read_only=True,
                )
            )
            if exists:
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_TABLE}
                        SET identity_text = :identity_text,
                            host_id = :host_id,
                            state = :state,
                            quantity = :quantity,
                            max_quantity = :max_quantity,
                            max_concurrent = :max_concurrent,
                            fence_value = :fence_value,
                            created_at = :created_at,
                            lease_text = :lease_text
                        WHERE lease_id = :lease_id""",
                        parameters,
                    )
                )
            else:
                changed = transaction.execute(self._insert_statement(parameters))
            if changed != 1:
                raise _integrity(ValueError("resource lease save affected an invalid row count"))

    def reserve(self, lease: Lease) -> LeaseAdmissionResult:
        if lease.state is not LeaseState.RESERVED:
            raise ValueError("SQLite lease reservation requires reserved state")
        identity_text = _identity_text(lease.budget.identity)
        with self._database.transaction() as transaction:
            duplicate = transaction.query(
                DatabaseStatement(
                    f"SELECT lease_id FROM {_TABLE} WHERE lease_id = :lease_id",
                    {"lease_id": lease.id},
                    read_only=True,
                )
            )
            rows = transaction.query(
                DatabaseStatement(
                    f"""SELECT quantity, max_quantity, max_concurrent
                    FROM {_TABLE}
                    WHERE identity_text = :identity_text
                      AND state IN (:reserved_state, :active_state)""",
                    {
                        "identity_text": identity_text,
                        "reserved_state": _CONSUMING_STATES[0],
                        "active_state": _CONSUMING_STATES[1],
                    },
                    read_only=True,
                )
            )
            try:
                used_quantity = sum(_stored_float(row["quantity"], "quantity") for row in rows)
                max_quantity = min(
                    (
                        lease.budget.max_quantity,
                        *(_stored_float(row["max_quantity"], "maximum quantity") for row in rows),
                    )
                )
                concurrency_limits = [
                    limit
                    for limit in (
                        lease.budget.max_concurrent,
                        *(
                            _stored_optional_int(row["max_concurrent"], "maximum concurrency")
                            for row in rows
                        ),
                    )
                    if limit is not None
                ]
            except (KeyError, TypeError, ValueError) as error:
                raise _integrity(error) from None
            max_concurrent = min(concurrency_limits) if concurrency_limits else None
            available_quantity = max(max_quantity - used_quantity, 0.0)
            concurrent = len(rows)
            if duplicate:
                return LeaseAdmissionResult(
                    LeaseAdmissionStatus.DUPLICATE,
                    available_quantity,
                    concurrent,
                    max_concurrent,
                )
            if lease.quantity > available_quantity:
                return LeaseAdmissionResult(
                    LeaseAdmissionStatus.CAPACITY_EXCEEDED,
                    available_quantity,
                    concurrent,
                    max_concurrent,
                )
            if max_concurrent is not None and concurrent >= max_concurrent:
                return LeaseAdmissionResult(
                    LeaseAdmissionStatus.CONCURRENCY_EXCEEDED,
                    available_quantity,
                    concurrent,
                    max_concurrent,
                )
            changed = transaction.execute(self._insert_statement(self._parameters(lease)))
            if changed != 1:
                raise _integrity(ValueError("resource lease reserve affected an invalid row count"))
            return LeaseAdmissionResult(
                LeaseAdmissionStatus.ADMITTED,
                available_quantity - lease.quantity,
                concurrent + 1,
                max_concurrent,
            )

    def compare_and_swap(self, lease: Lease, *, expected_fence: int) -> bool:
        if type(expected_fence) is not int or expected_fence < 0:
            raise ValueError("resource lease expected fence is invalid")
        parameters = self._parameters(lease)
        parameters["expected_fence"] = expected_fence
        with self._database.transaction() as transaction:
            changed = transaction.execute(
                DatabaseStatement(
                    f"""UPDATE {_TABLE}
                    SET identity_text = :identity_text,
                        host_id = :host_id,
                        state = :state,
                        quantity = :quantity,
                        max_quantity = :max_quantity,
                        max_concurrent = :max_concurrent,
                        fence_value = :fence_value,
                        created_at = :created_at,
                        lease_text = :lease_text
                    WHERE lease_id = :lease_id AND fence_value = :expected_fence""",
                    parameters,
                )
            )
        if changed not in {0, 1}:
            raise _integrity(ValueError("resource lease CAS affected an invalid row count"))
        return changed == 1

    def get(self, lease_id: str) -> Lease | None:
        with self._database.transaction(read_only=True) as transaction:
            rows = transaction.query(
                DatabaseStatement(
                    f"SELECT lease_text FROM {_TABLE} WHERE lease_id = :lease_id",
                    {"lease_id": lease_id},
                    read_only=True,
                )
            )
        if not rows:
            return None
        if len(rows) != 1:
            raise _integrity(ValueError("resource lease lookup returned duplicate rows"))
        return self._decode_row(rows[0])

    def list(
        self,
        host_id: str | None = None,
        state: str | None = None,
    ) -> list[Lease]:
        where: list[str] = []
        parameters: dict[str, object] = {}
        if host_id is not None:
            where.append("host_id = :host_id")
            parameters["host_id"] = host_id
        if state is not None:
            where.append("state = :state")
            parameters["state"] = state
        condition = "" if not where else " WHERE " + " AND ".join(where)
        with self._database.transaction(read_only=True) as transaction:
            rows = transaction.query(
                DatabaseStatement(
                    f"""SELECT lease_text FROM {_TABLE}{condition}
                    ORDER BY created_at ASC, lease_id ASC""",
                    parameters,
                    read_only=True,
                )
            )
        return [self._decode_row(row) for row in rows]

    def delete(self, lease_id: str) -> bool:
        with self._database.transaction() as transaction:
            changed = transaction.execute(
                DatabaseStatement(
                    f"DELETE FROM {_TABLE} WHERE lease_id = :lease_id",
                    {"lease_id": lease_id},
                )
            )
        if changed not in {0, 1}:
            raise _integrity(ValueError("resource lease delete affected an invalid row count"))
        return changed == 1

    def _install(self) -> None:
        with self._database.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                        lease_id VARCHAR(200) PRIMARY KEY,
                        identity_text TEXT NOT NULL,
                        host_id VARCHAR(200) NOT NULL,
                        state VARCHAR(32) NOT NULL,
                        quantity REAL NOT NULL,
                        max_quantity REAL NOT NULL,
                        max_concurrent INTEGER NULL,
                        fence_value INTEGER NULL,
                        created_at VARCHAR(32) NOT NULL,
                        lease_text TEXT NOT NULL
                    )"""
                )
            )

    @staticmethod
    def _parameters(lease: Lease) -> dict[str, object]:
        return {
            "created_at": _timestamp(lease.created_at),
            "fence_value": None if lease.fence_token is None else lease.fence_token.value,
            "host_id": lease.budget.identity.host_id,
            "identity_text": _identity_text(lease.budget.identity),
            "lease_id": lease.id,
            "lease_text": _lease_text(lease),
            "max_concurrent": lease.budget.max_concurrent,
            "max_quantity": lease.budget.max_quantity,
            "quantity": lease.quantity,
            "state": lease.state.value,
        }

    @staticmethod
    def _insert_statement(parameters: dict[str, object]) -> DatabaseStatement:
        return DatabaseStatement(
            f"""INSERT INTO {_TABLE} (
                lease_id,
                identity_text,
                host_id,
                state,
                quantity,
                max_quantity,
                max_concurrent,
                fence_value,
                created_at,
                lease_text
            ) VALUES (
                :lease_id,
                :identity_text,
                :host_id,
                :state,
                :quantity,
                :max_quantity,
                :max_concurrent,
                :fence_value,
                :created_at,
                :lease_text
            )""",
            parameters,
        )

    @staticmethod
    def _decode_row(row: DatabaseRow) -> Lease:
        try:
            return _lease_from_text(row["lease_text"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise _integrity(error) from None
