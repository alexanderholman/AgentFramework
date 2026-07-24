"""Deterministic relational migration planning, ledgers, and lease locks."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import islice
from types import TracebackType
from typing import Literal

from af.framework.errors import PlatformError, PlatformReason
from af.framework.relational import (
    DatabaseDriver,
    DatabaseFeature,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    DatabaseTransaction,
)

__all__ = (
    "Migration",
    "MigrationDirection",
    "MigrationIntegrityError",
    "MigrationIrreversibleError",
    "MigrationLock",
    "MigrationLockUnavailable",
    "MigrationPlan",
    "MigrationProbe",
    "MigrationRunner",
    "MigrationVerificationError",
)

_MIGRATION_ID = re.compile(r"^[0-9]{14}_[a-z0-9]+(?:_[a-z0-9]+)*$")
_OWNER_ID = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_LEDGER = "af_migrations"
_LOCKS = "af_migration_locks"
_LOCK_NAME = "schema"
_MAX_MIGRATIONS = 10_000
_MAX_STATEMENTS = 1_000
_MAX_PROBES = 256
_MAX_EXPECTED_ROWS = 1_000
_MAX_PLAN_LIMIT = 10_000
_MAX_DOWN_STEPS = 1_000
_MAX_LEASE = timedelta(hours=24)


class MigrationDirection(StrEnum):
    """The only supported deterministic migration directions."""

    UP = "up"
    DOWN = "down"


class MigrationIntegrityError(DatabaseOperationError):
    """The migration catalogue or ledger is internally inconsistent."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            reason=PlatformReason.INTEGRITY_FAILURE,
            stage="migration.integrity",
            protected_cause=protected_cause,
        )


class MigrationVerificationError(DatabaseOperationError):
    """An applied migration no longer satisfies its declared probes."""

    def __init__(self, migration_id: str, *, protected_cause: BaseException | None = None) -> None:
        self.migration_id = migration_id
        super().__init__(
            reason=PlatformReason.INTEGRITY_FAILURE,
            stage="migration.verify",
            protected_cause=protected_cause,
        )


class MigrationLockUnavailable(DatabaseOperationError):
    """Another migration owner currently holds the schema lease."""

    def __init__(self, *, protected_cause: BaseException | None = None) -> None:
        super().__init__(
            reason=PlatformReason.TRANSIENT_FAILURE,
            stage="migration.lock",
            protected_cause=protected_cause,
        )


class MigrationIrreversibleError(DatabaseOperationError):
    """A requested down migration has no declared reversal statements."""

    def __init__(self, migration_id: str) -> None:
        self.migration_id = migration_id
        super().__init__(
            reason=PlatformReason.VALIDATION_FAILED,
            stage="migration.down",
        )


@dataclass(frozen=True, slots=True, repr=False)
class MigrationProbe:
    """One exact read-only assertion evaluated after migration application."""

    statement: DatabaseStatement
    expected_rows: tuple[DatabaseRow, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.statement, DatabaseStatement) or not self.statement.read_only:
            raise ValueError("migration probes require read-only DatabaseStatement values")
        if self.statement.parameters:
            raise ValueError("migration probe statements cannot contain parameters")
        if (
            type(self.expected_rows) is not tuple
            or len(self.expected_rows) > _MAX_EXPECTED_ROWS
            or any(not isinstance(row, DatabaseRow) for row in self.expected_rows)
        ):
            raise ValueError("migration probe expected rows must be a bounded exact tuple")

    def __repr__(self) -> str:
        return f"MigrationProbe(expected_row_count={len(self.expected_rows)!r})"


@dataclass(frozen=True, slots=True, repr=False)
class Migration:
    """One immutable ordered schema transition and its exact checksum identity."""

    migration_id: str
    up: tuple[DatabaseStatement, ...]
    down: tuple[DatabaseStatement, ...] = ()
    verify: tuple[MigrationProbe, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.migration_id) is not str
            or len(self.migration_id) > 128
            or _MIGRATION_ID.fullmatch(self.migration_id) is None
        ):
            raise ValueError("migration id must be a canonical timestamped identifier")
        _write_statements(self.up, field="up", required=True)
        _write_statements(self.down, field="down", required=False)
        if (
            type(self.verify) is not tuple
            or len(self.verify) > _MAX_PROBES
            or any(not isinstance(probe, MigrationProbe) for probe in self.verify)
        ):
            raise ValueError("migration verification probes are invalid or too numerous")

    @property
    def checksum(self) -> str:
        payload = {
            "down": [statement.sql for statement in self.down],
            "migration_id": self.migration_id,
            "up": [statement.sql for statement in self.up],
            "verify": [
                {
                    "expected": [
                        {
                            "columns": list(row.columns),
                            "values": [_json_value(value) for value in row.values],
                        }
                        for row in probe.expected_rows
                    ],
                    "sql": probe.statement.sql,
                }
                for probe in self.verify
            ],
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def __repr__(self) -> str:
        return (
            f"Migration(migration_id={self.migration_id!r}, "
            f"up_count={len(self.up)!r}, down_count={len(self.down)!r}, "
            f"verify_count={len(self.verify)!r}, checksum={self.checksum!r})"
        )


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """A deterministic immutable plan safe to show without exposing SQL."""

    direction: MigrationDirection
    migration_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.direction, MigrationDirection):
            raise TypeError("migration plan direction is invalid")
        if type(self.migration_ids) is not tuple or any(
            type(value) is not str or _MIGRATION_ID.fullmatch(value) is None
            for value in self.migration_ids
        ):
            raise ValueError("migration plan identifiers are invalid")

    @property
    def empty(self) -> bool:
        return not self.migration_ids


@dataclass(frozen=True, slots=True)
class _AppliedMigration:
    migration_id: str
    checksum: str
    batch: int
    applied_order: int
    applied_at: datetime


class MigrationLock(AbstractContextManager["MigrationLock"]):
    """A finite database-backed ownership lease for schema mutation."""

    def __init__(
        self,
        driver: DatabaseDriver,
        *,
        owner_id: str,
        clock: Callable[[], datetime],
        lease: timedelta,
    ) -> None:
        if not _owner_id(owner_id):
            raise ValueError("migration lock owner id is invalid")
        if not isinstance(lease, timedelta) or lease <= timedelta(0) or lease > _MAX_LEASE:
            raise ValueError("migration lock lease is invalid")
        self._driver = driver
        self._owner_id = owner_id
        self._clock = clock
        self._lease = lease
        self._held = False
        self._finished = False
        self._last_observed_at: datetime | None = None
        self._expires_at: str | None = None

    def __enter__(self) -> MigrationLock:
        if self._held or self._finished:
            raise MigrationLockUnavailable()
        self._finished = True
        now = _aware_utc(self._clock(), "migration lock clock")
        expires_at = now + self._lease
        try:
            with self._driver.transaction() as transaction:
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_LOCKS}
                        SET owner_id = :owner_id,
                            acquired_at = :acquired_at,
                            expires_at = :expires_at
                        WHERE lock_name = :lock_name
                          AND expires_at <= :now""",
                        {
                            "owner_id": self._owner_id,
                            "acquired_at": _timestamp(now),
                            "expires_at": _timestamp(expires_at),
                            "lock_name": _LOCK_NAME,
                            "now": _timestamp(now),
                        },
                    )
                )
                if _one_or_zero(changed, stage="migration.lock") == 0:
                    inserted = transaction.execute(
                        DatabaseStatement(
                            f"""INSERT INTO {_LOCKS} (
                                lock_name,
                                owner_id,
                                acquired_at,
                                expires_at
                            ) VALUES (
                                :lock_name,
                                :owner_id,
                                :acquired_at,
                                :expires_at
                            )""",
                            {
                                "lock_name": _LOCK_NAME,
                                "owner_id": self._owner_id,
                                "acquired_at": _timestamp(now),
                                "expires_at": _timestamp(expires_at),
                            },
                        )
                    )
                    if _one_or_zero(inserted, stage="migration.lock") != 1:
                        raise MigrationIntegrityError()
                rows = transaction.query(
                    DatabaseStatement(
                        f"""SELECT owner_id, expires_at
                        FROM {_LOCKS}
                        WHERE lock_name = :lock_name""",
                        {"lock_name": _LOCK_NAME},
                        read_only=True,
                    )
                )
                expires_token = _timestamp(expires_at)
                if (
                    len(rows) != 1
                    or rows[0]["owner_id"] != self._owner_id
                    or rows[0]["expires_at"] != expires_token
                ):
                    raise MigrationIntegrityError()
        except MigrationIntegrityError:
            raise
        except DatabaseOperationError as error:
            raise MigrationLockUnavailable(protected_cause=error) from None
        self._held = True
        self._last_observed_at = now
        self._expires_at = expires_token
        return self

    def renew(self) -> None:
        """Extend ownership only while this exact lease token remains authoritative."""

        current_token = self._expires_at
        if not self._held or current_token is None:
            raise MigrationLockUnavailable()
        now = _aware_utc(self._clock(), "migration lock clock")
        last = self._last_observed_at
        if last is not None and now < last:
            raise MigrationIntegrityError(
                protected_cause=ValueError("migration lock clock moved backwards")
            )
        current_expiry = _parse_timestamp(current_token)
        next_expiry = max(now + self._lease, current_expiry + timedelta(microseconds=1))
        next_token = _timestamp(next_expiry)
        try:
            with self._driver.transaction() as transaction:
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""UPDATE {_LOCKS}
                        SET expires_at = :next_expires_at
                        WHERE lock_name = :lock_name
                          AND owner_id = :owner_id
                          AND expires_at = :current_expires_at
                          AND expires_at > :now""",
                        {
                            "next_expires_at": next_token,
                            "lock_name": _LOCK_NAME,
                            "owner_id": self._owner_id,
                            "current_expires_at": current_token,
                            "now": _timestamp(now),
                        },
                    )
                )
                if _one_or_zero(changed, stage="migration.lock") != 1:
                    self._held = False
                    self._expires_at = None
                    raise MigrationLockUnavailable()
        except (MigrationIntegrityError, MigrationLockUnavailable):
            raise
        except DatabaseOperationError as error:
            self._held = False
            self._expires_at = None
            raise MigrationLockUnavailable(protected_cause=error) from None
        self._last_observed_at = now
        self._expires_at = next_token

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        del error_type, error, traceback
        expires_token = self._expires_at
        if not self._held or expires_token is None:
            return False
        try:
            with self._driver.transaction() as transaction:
                changed = transaction.execute(
                    DatabaseStatement(
                        f"""DELETE FROM {_LOCKS}
                        WHERE lock_name = :lock_name
                          AND owner_id = :owner_id
                          AND expires_at = :expires_at""",
                        {
                            "lock_name": _LOCK_NAME,
                            "owner_id": self._owner_id,
                            "expires_at": expires_token,
                        },
                    )
                )
                if _one_or_zero(changed, stage="migration.lock") != 1:
                    raise MigrationIntegrityError()
        finally:
            self._held = False
            self._expires_at = None
        return False


class MigrationRunner:
    """Plan, apply, reverse, and verify an immutable migration catalogue."""

    def __init__(
        self,
        driver: DatabaseDriver,
        migrations: Iterable[Migration],
        *,
        owner_id: str,
        clock: Callable[[], datetime],
        lock_lease: timedelta = timedelta(minutes=5),
    ) -> None:
        if not _owner_id(owner_id):
            raise ValueError("migration runner owner id is invalid")
        if not callable(clock):
            raise TypeError("migration runner clock must be callable")
        if (
            not isinstance(lock_lease, timedelta)
            or lock_lease <= timedelta(0)
            or lock_lease > _MAX_LEASE
        ):
            raise ValueError("migration runner lock lease is invalid")
        snapshot = tuple(islice(iter(migrations), _MAX_MIGRATIONS + 1))
        if len(snapshot) > _MAX_MIGRATIONS or any(
            not isinstance(migration, Migration) for migration in snapshot
        ):
            raise ValueError("migration catalogue is invalid or too large")
        ordered = tuple(sorted(snapshot, key=lambda migration: migration.migration_id))
        if len({migration.migration_id for migration in ordered}) != len(ordered):
            raise MigrationIntegrityError(protected_cause=ValueError("duplicate migration id"))
        driver.require(DatabaseFeature.TRANSACTIONS)
        driver.require(DatabaseFeature.STREAMING)
        self._driver = driver
        self._migrations = ordered
        self._by_id = {migration.migration_id: migration for migration in ordered}
        self._owner_id = owner_id
        self._clock = clock
        self._lock_lease = lock_lease

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return self._migrations

    def bootstrap(self) -> None:
        """Create the fixed migration ledger and lease tables idempotently."""

        with self._driver.transaction() as transaction:
            transaction.execute(
                DatabaseStatement(
                    f"""CREATE TABLE IF NOT EXISTS {_LEDGER} (
                        migration_id VARCHAR(128) PRIMARY KEY,
                        checksum VARCHAR(64) NOT NULL,
                        batch INTEGER NOT NULL,
                        applied_order INTEGER NOT NULL UNIQUE,
                        applied_at VARCHAR(32) NOT NULL
                    )"""
                )
            )
            transaction.execute(
                DatabaseStatement(
                    f"""CREATE TABLE IF NOT EXISTS {_LOCKS} (
                        lock_name VARCHAR(128) PRIMARY KEY,
                        owner_id VARCHAR(128) NOT NULL,
                        acquired_at VARCHAR(32) NOT NULL,
                        expires_at VARCHAR(32) NOT NULL
                    )"""
                )
            )

    def lock(self) -> MigrationLock:
        """Return a fresh single-use schema lease context."""

        return MigrationLock(
            self._driver,
            owner_id=self._owner_id,
            clock=self._clock,
            lease=self._lock_lease,
        )

    def plan_up(self, *, limit: int | None = None) -> MigrationPlan:
        applied = self._read_applied()
        start = len(applied)
        remaining = self._migrations[start:]
        if limit is not None:
            remaining = remaining[: _plan_limit(limit)]
        return MigrationPlan(
            MigrationDirection.UP,
            tuple(migration.migration_id for migration in remaining),
        )

    def plan_down(self, *, steps: int = 1) -> MigrationPlan:
        count = _down_steps(steps)
        applied = self._read_applied()
        selected = tuple(reversed(applied[-count:]))
        return MigrationPlan(
            MigrationDirection.DOWN,
            tuple(item.migration_id for item in selected),
        )

    def apply_up(
        self,
        *,
        limit: int | None = None,
        dry_run: bool = False,
    ) -> MigrationPlan:
        if type(dry_run) is not bool:
            raise ValueError("migration dry-run flag must be boolean")
        if dry_run:
            return self.plan_up(limit=limit)
        with self.lock() as lease:
            plan = self.plan_up(limit=limit)
            applied = self._read_applied()
            batch = 1 + max((item.batch for item in applied), default=0)
            applied_order = len(applied)
            for migration_id in plan.migration_ids:
                migration = self._by_id[migration_id]
                applied_order += 1
                lease.renew()
                with self._driver.transaction() as transaction:
                    for statement in migration.up:
                        transaction.execute(statement)
                    self._verify_migration(transaction, migration)
                    transaction.execute(
                        DatabaseStatement(
                            f"""INSERT INTO {_LEDGER} (
                                migration_id,
                                checksum,
                                batch,
                                applied_order,
                                applied_at
                            ) VALUES (
                                :migration_id,
                                :checksum,
                                :batch,
                                :applied_order,
                                :applied_at
                            )""",
                            {
                                "migration_id": migration.migration_id,
                                "checksum": migration.checksum,
                                "batch": batch,
                                "applied_order": applied_order,
                                "applied_at": _timestamp(
                                    _aware_utc(self._clock(), "migration clock")
                                ),
                            },
                        )
                    )
                lease.renew()
            return plan

    def apply_down(
        self,
        *,
        steps: int = 1,
        dry_run: bool = False,
    ) -> MigrationPlan:
        if type(dry_run) is not bool:
            raise ValueError("migration dry-run flag must be boolean")
        if dry_run:
            return self.plan_down(steps=steps)
        with self.lock() as lease:
            plan = self.plan_down(steps=steps)
            selected = tuple(self._by_id[migration_id] for migration_id in plan.migration_ids)
            for migration in selected:
                if not migration.down:
                    raise MigrationIrreversibleError(migration.migration_id)
            for migration in selected:
                migration_id = migration.migration_id
                lease.renew()
                with self._driver.transaction() as transaction:
                    for statement in migration.down:
                        transaction.execute(statement)
                    changed = transaction.execute(
                        DatabaseStatement(
                            f"DELETE FROM {_LEDGER} WHERE migration_id = :migration_id",
                            {"migration_id": migration_id},
                        )
                    )
                    if _one_or_zero(changed, stage="migration.down") != 1:
                        raise MigrationIntegrityError()
                lease.renew()
            return plan

    def verify(self) -> tuple[str, ...]:
        applied = self._read_applied()
        with self._driver.transaction(read_only=True) as transaction:
            for item in applied:
                self._verify_migration(transaction, self._by_id[item.migration_id])
        return tuple(item.migration_id for item in applied)

    def _read_applied(self) -> tuple[_AppliedMigration, ...]:
        try:
            with self._driver.transaction(read_only=True) as transaction:
                rows = _bounded_rows(
                    transaction,
                    DatabaseStatement(
                        f"""SELECT
                            migration_id,
                            checksum,
                            batch,
                            applied_order,
                            applied_at
                        FROM {_LEDGER}
                        ORDER BY applied_order ASC""",
                        read_only=True,
                    ),
                    maximum=len(self._migrations),
                )
        except DatabaseOperationError:
            raise
        try:
            applied = tuple(_applied(row) for row in rows)
            if len(applied) > len(self._migrations):
                raise ValueError("migration ledger is longer than catalogue")
            expected_ids = tuple(
                migration.migration_id for migration in self._migrations[: len(applied)]
            )
            actual_ids = tuple(item.migration_id for item in applied)
            if actual_ids != expected_ids:
                raise ValueError("migration ledger is not a catalogue prefix")
            for expected_order, item in enumerate(applied, start=1):
                migration = self._by_id[item.migration_id]
                if (
                    item.applied_order != expected_order
                    or item.batch < 1
                    or item.checksum != migration.checksum
                ):
                    raise ValueError("migration ledger metadata diverges")
        except (KeyError, TypeError, ValueError) as error:
            raise MigrationIntegrityError(protected_cause=error) from None
        return applied

    @staticmethod
    def _verify_migration(transaction: DatabaseTransaction, migration: Migration) -> None:
        for probe in migration.verify:
            try:
                actual = _bounded_rows(
                    transaction,
                    probe.statement,
                    maximum=len(probe.expected_rows),
                )
            except DatabaseOperationError:
                raise
            if actual != probe.expected_rows:
                raise MigrationVerificationError(
                    migration.migration_id,
                    protected_cause=ValueError("migration probe result mismatch"),
                )


def _bounded_rows(
    transaction: DatabaseTransaction,
    statement: DatabaseStatement,
    *,
    maximum: int,
) -> tuple[DatabaseRow, ...]:
    cursor = transaction.stream(statement)
    primary_error: BaseException | None = None
    try:
        return tuple(islice(cursor, maximum + 1))
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            cursor.close()
        except BaseException as close_error:
            if primary_error is not None:
                if _control_flow_rank(close_error) > _control_flow_rank(primary_error):
                    raise
            elif isinstance(close_error, DatabaseOperationError):
                raise
            elif isinstance(close_error, Exception):
                raise DatabaseOperationError(
                    stage="database.cursor-close",
                    protected_cause=close_error,
                ) from None
            else:
                raise


def _control_flow_rank(error: BaseException) -> int:
    if not isinstance(error, Exception):
        return 3
    if isinstance(error, PlatformError):
        if error.reason is PlatformReason.CANCELLED:
            return 2
        if error.reason is PlatformReason.SHUTDOWN_FAILED:
            return 1
    return 0


def _write_statements(
    statements: tuple[DatabaseStatement, ...],
    *,
    field: str,
    required: bool,
) -> None:
    if (
        type(statements) is not tuple
        or (required and not statements)
        or len(statements) > _MAX_STATEMENTS
        or any(
            not isinstance(statement, DatabaseStatement)
            or statement.read_only
            or bool(statement.parameters)
            for statement in statements
        )
    ):
        raise ValueError(f"migration {field} statements are invalid")


def _json_value(value: object) -> object:
    if isinstance(value, (datetime,)):
        return {"datetime": _timestamp(_aware_utc(value, "migration expected datetime"))}
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if hasattr(value, "isoformat") and not isinstance(value, (str, bool, int, float)):
        return {"date": value.isoformat()}
    return value


def _owner_id(value: object) -> bool:
    return type(value) is str and _OWNER_ID.fullmatch(value) is not None


def _plan_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_PLAN_LIMIT:
        raise ValueError("migration plan limit is invalid")
    return value


def _down_steps(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_DOWN_STEPS:
        raise ValueError("migration down steps are invalid")
    return value


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("migration timestamp is not canonical UTC")
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    normalized = _aware_utc(parsed, "migration stored time")
    if _timestamp(normalized) != value:
        raise ValueError("migration timestamp is not canonical UTC")
    return normalized


def _stored_text(value: object) -> str:
    if type(value) is not str:
        raise TypeError("migration stored text is invalid")
    return value


def _stored_int(value: object) -> int:
    if type(value) is not int:
        raise TypeError("migration stored integer is invalid")
    return value


def _applied(row: DatabaseRow) -> _AppliedMigration:
    migration_id = _stored_text(row["migration_id"])
    checksum = _stored_text(row["checksum"])
    if (
        _MIGRATION_ID.fullmatch(migration_id) is None
        or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
    ):
        raise ValueError("migration ledger identity is invalid")
    return _AppliedMigration(
        migration_id=migration_id,
        checksum=checksum,
        batch=_stored_int(row["batch"]),
        applied_order=_stored_int(row["applied_order"]),
        applied_at=_parse_timestamp(row["applied_at"]),
    )


def _one_or_zero(value: int, *, stage: str) -> int:
    if type(value) is not int or value not in (0, 1):
        raise MigrationIntegrityError(protected_cause=ValueError(f"{stage} row count is invalid"))
    return value
