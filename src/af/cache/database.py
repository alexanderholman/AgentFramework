"""Verified database-backed cache driver with exact namespace isolation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import StaticPool

from af.cache.contracts import (
    CacheKey,
    CacheNamespace,
    LockLease,
    LockOwner,
    _validated_operation,
    _validated_ttl,
    _validated_value,
)
from af.cache.manager import CacheDriver, CacheFeature
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)

__all__ = ("DatabaseCacheDriver", "database_cache_registration")

_CAPABILITIES = CapabilitySet(
    (
        CacheFeature.READ.value,
        CacheFeature.WRITE.value,
        CacheFeature.TTL.value,
        CacheFeature.NAMESPACES.value,
        CacheFeature.FLUSH.value,
        CacheFeature.LOCKS.value,
    )
)
_ARTIFACT_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(
    f"python:sqlite3:{sqlite3.sqlite_version}:sqlalchemy".encode()
).hexdigest()


def _failure(error: BaseException, *, stage: str) -> PlatformError:
    return PlatformError(
        PlatformReason.PERMANENT_FAILURE,
        stage=stage,
        protected_cause=error,
    )


class DatabaseCacheDriver:
    """Persistent cache rows stored in one local SQL database."""

    def __init__(
        self,
        *,
        url: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(url) is not str or not url:
            raise ValueError("database cache URL must be a non-empty string")
        parsed = make_url(url)
        engine_options: dict[str, object] = {}
        if parsed.get_backend_name() == "sqlite":
            engine_options["connect_args"] = {"check_same_thread": False}
            database_name = parsed.database
            if database_name in (None, "", ":memory:"):
                engine_options["poolclass"] = StaticPool
            elif database_name is not None:
                Path(database_name).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._engine: Engine = create_engine(url, **engine_options)
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("database cache clock must be callable")
        self._lock = RLock()
        self._closed = False
        self._create_schema()

    def _create_schema(self) -> None:
        statement = text(
            "CREATE TABLE IF NOT EXISTS af_cache_entries ("
            "application_id TEXT NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "namespace TEXT NOT NULL, "
            "key TEXT NOT NULL, "
            "value BLOB NOT NULL, "
            "expires_at TEXT NULL, "
            "PRIMARY KEY (application_id, tenant_id, namespace, key)"
            ")"
        )
        index = text(
            "CREATE INDEX IF NOT EXISTS af_cache_entries_expiry_idx "
            "ON af_cache_entries (application_id, tenant_id, namespace, expires_at)"
        )
        lock_table = text(
            "CREATE TABLE IF NOT EXISTS af_cache_locks ("
            "application_id TEXT NOT NULL, "
            "tenant_id TEXT NOT NULL, "
            "namespace TEXT NOT NULL, "
            "key TEXT NOT NULL, "
            "owner TEXT NOT NULL, "
            "fencing_token INTEGER NOT NULL, "
            "expires_at TEXT NOT NULL, "
            "PRIMARY KEY (application_id, tenant_id, namespace, key)"
            ")"
        )
        lock_index = text(
            "CREATE INDEX IF NOT EXISTS af_cache_locks_expiry_idx "
            "ON af_cache_locks (application_id, tenant_id, namespace, expires_at)"
        )
        try:
            with self._engine.begin() as connection:
                connection.execute(statement)
                connection.execute(index)
                connection.execute(lock_table)
                connection.execute(lock_index)
        except SQLAlchemyError as error:
            raise _failure(error, stage="cache.database.schema") from None

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="cache.database.clock",
            )
        return value.astimezone(UTC)

    def _assert_open(self) -> None:
        if self._closed:
            raise PlatformError(
                PlatformReason.SHUTDOWN_FAILED,
                stage="cache.database.connection",
            )

    @staticmethod
    def _expiry(value: str | None) -> datetime | None:
        if value is None:
            return None
        expiry = datetime.fromisoformat(value)
        if expiry.utcoffset() is None:
            raise ValueError("database cache expiry must be timezone-aware")
        return expiry.astimezone(UTC)

    @staticmethod
    def _lease(
        *,
        owner: object,
        fencing_token: object,
        expires_at: object,
    ) -> LockLease:
        if type(owner) is not str or type(fencing_token) is not int or type(expires_at) is not str:
            raise ValueError("database lock row is invalid")
        expiry = DatabaseCacheDriver._expiry(expires_at)
        if expiry is None:
            raise ValueError("database lock expiry is required")
        return LockLease(
            owner=LockOwner(owner),
            fencing_token=fencing_token,
            expires_at=expiry,
        )

    def get(self, namespace: CacheNamespace, key: CacheKey, /) -> bytes | None:
        namespace, key = _validated_operation(namespace, key)
        with self._lock:
            self._assert_open()
            try:
                with self._engine.begin() as connection:
                    row = (
                        connection.execute(
                            text(
                                "SELECT value, expires_at FROM af_cache_entries "
                                "WHERE application_id = :application_id AND tenant_id = :tenant_id "
                                "AND namespace = :namespace AND key = :key"
                            ),
                            {
                                "application_id": namespace.application_id,
                                "tenant_id": namespace.tenant_id,
                                "namespace": namespace.name,
                                "key": key.value,
                            },
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if row is None:
                        return None
                    try:
                        value = _validated_value(bytes(row["value"]))
                        expires_at = self._expiry(row["expires_at"])
                        if expires_at is None or expires_at > self._now():
                            return value
                    except (TypeError, ValueError):
                        pass
                    # A separate driver can replace this row after our read.
                    # Expiry/corruption cleanup may remove only that exact snapshot.
                    connection.execute(
                        text(
                            "DELETE FROM af_cache_entries "
                            "WHERE application_id = :application_id "
                            "AND tenant_id = :tenant_id AND namespace = :namespace "
                            "AND key = :key AND value = :observed_value "
                            "AND (expires_at = :observed_expiry "
                            "OR (expires_at IS NULL AND :observed_expiry IS NULL))"
                        ),
                        {
                            "application_id": namespace.application_id,
                            "tenant_id": namespace.tenant_id,
                            "namespace": namespace.name,
                            "key": key.value,
                            "observed_value": row["value"],
                            "observed_expiry": row["expires_at"],
                        },
                    )
                    return None
            except SQLAlchemyError as error:
                raise _failure(error, stage="cache.database.get") from None

    def set(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        value: bytes,
        /,
        *,
        ttl: timedelta | None = None,
    ) -> None:
        namespace, key = _validated_operation(namespace, key)
        value = _validated_value(value)
        ttl = _validated_ttl(ttl)
        with self._lock:
            self._assert_open()
            expires_at = None if ttl is None else (self._now() + ttl).isoformat()
            try:
                with self._engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO af_cache_entries "
                            "(application_id, tenant_id, namespace, key, value, expires_at) "
                            "VALUES ("
                            ":application_id, :tenant_id, :namespace, :key, :value, :expires_at"
                            ") "
                            "ON CONFLICT(application_id, tenant_id, namespace, key) DO UPDATE SET "
                            "value = excluded.value, expires_at = excluded.expires_at"
                        ),
                        {
                            "application_id": namespace.application_id,
                            "tenant_id": namespace.tenant_id,
                            "namespace": namespace.name,
                            "key": key.value,
                            "value": value,
                            "expires_at": expires_at,
                        },
                    )
            except SQLAlchemyError as error:
                raise _failure(error, stage="cache.database.set") from None

    def delete(self, namespace: CacheNamespace, key: CacheKey, /) -> bool:
        namespace, key = _validated_operation(namespace, key)
        with self._lock:
            self._assert_open()
            try:
                with self._engine.begin() as connection:
                    deleted = connection.execute(
                        text(
                            "DELETE FROM af_cache_entries WHERE application_id = :application_id "
                            "AND tenant_id = :tenant_id AND namespace = :namespace AND key = :key"
                        ),
                        {
                            "application_id": namespace.application_id,
                            "tenant_id": namespace.tenant_id,
                            "namespace": namespace.name,
                            "key": key.value,
                        },
                    )
                    return deleted.rowcount > 0
            except SQLAlchemyError as error:
                raise _failure(error, stage="cache.database.delete") from None

    def flush(self, namespace: CacheNamespace, /) -> int:
        if not isinstance(namespace, CacheNamespace):
            raise TypeError("cache flush requires a validated namespace")
        with self._lock:
            self._assert_open()
            try:
                with self._engine.begin() as connection:
                    deleted = connection.execute(
                        text(
                            "DELETE FROM af_cache_entries WHERE application_id = :application_id "
                            "AND tenant_id = :tenant_id AND namespace = :namespace"
                        ),
                        {
                            "application_id": namespace.application_id,
                            "tenant_id": namespace.tenant_id,
                            "namespace": namespace.name,
                        },
                    )
                    return deleted.rowcount
            except SQLAlchemyError as error:
                raise _failure(error, stage="cache.database.flush") from None

    def acquire_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        owner: LockOwner,
        /,
        *,
        ttl: timedelta,
    ) -> LockLease | None:
        namespace, key = _validated_operation(namespace, key)
        if not isinstance(owner, LockOwner):
            raise TypeError("lock acquire requires a validated owner")
        validated_ttl = _validated_ttl(ttl)
        if validated_ttl is None:
            raise ValueError("lock TTL is required")
        with self._lock:
            self._assert_open()
            now = self._now()
            expires_at = now + validated_ttl
            try:
                with self._engine.begin() as connection:
                    row = (
                        connection.execute(
                            text(
                                "SELECT owner, fencing_token, expires_at FROM af_cache_locks "
                                "WHERE application_id = :application_id AND tenant_id = :tenant_id "
                                "AND namespace = :namespace AND key = :key"
                            ),
                            {
                                "application_id": namespace.application_id,
                                "tenant_id": namespace.tenant_id,
                                "namespace": namespace.name,
                                "key": key.value,
                            },
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if row is None:
                        created = connection.execute(
                            text(
                                "INSERT INTO af_cache_locks "
                                "(application_id, tenant_id, namespace, key, owner, "
                                "fencing_token, expires_at) "
                                "VALUES ("
                                ":application_id, :tenant_id, :namespace, :key, "
                                ":owner, 1, :expires_at"
                                ") "
                                "ON CONFLICT(application_id, tenant_id, namespace, key) DO NOTHING"
                            ),
                            {
                                "application_id": namespace.application_id,
                                "tenant_id": namespace.tenant_id,
                                "namespace": namespace.name,
                                "key": key.value,
                                "owner": owner.value,
                                "expires_at": expires_at.isoformat(),
                            },
                        )
                        if created.rowcount == 0:
                            return None
                        return LockLease(
                            owner=owner,
                            fencing_token=1,
                            expires_at=expires_at,
                        )

                    current = self._lease(
                        owner=row["owner"],
                        fencing_token=row["fencing_token"],
                        expires_at=row["expires_at"],
                    )
                    if current.expires_at > now:
                        return None
                    next_token = current.fencing_token + 1
                    claimed = connection.execute(
                        text(
                            "UPDATE af_cache_locks SET owner = :owner, "
                            "fencing_token = :next_token, "
                            "expires_at = :next_expires_at "
                            "WHERE application_id = :application_id AND tenant_id = :tenant_id "
                            "AND namespace = :namespace AND key = :key "
                            "AND owner = :current_owner AND fencing_token = :current_token "
                            "AND expires_at = :current_expires_at"
                        ),
                        {
                            "application_id": namespace.application_id,
                            "tenant_id": namespace.tenant_id,
                            "namespace": namespace.name,
                            "key": key.value,
                            "owner": owner.value,
                            "next_token": next_token,
                            "next_expires_at": expires_at.isoformat(),
                            "current_owner": current.owner.value,
                            "current_token": current.fencing_token,
                            "current_expires_at": current.expires_at.isoformat(),
                        },
                    )
                    if claimed.rowcount == 0:
                        return None
                    return LockLease(
                        owner=owner,
                        fencing_token=next_token,
                        expires_at=expires_at,
                    )
            except SQLAlchemyError as error:
                raise _failure(error, stage="cache.database.lock.acquire") from None

    def renew_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        lease: LockLease,
        /,
        *,
        ttl: timedelta,
    ) -> LockLease | None:
        namespace, key = _validated_operation(namespace, key)
        if not isinstance(lease, LockLease):
            raise TypeError("lock renewal requires a validated lease")
        validated_ttl = _validated_ttl(ttl)
        if validated_ttl is None:
            raise ValueError("lock TTL is required")
        with self._lock:
            self._assert_open()
            now = self._now()
            if lease.expires_at <= now:
                return None
            expires_at = now + validated_ttl
            try:
                with self._engine.begin() as connection:
                    renewed = connection.execute(
                        text(
                            "UPDATE af_cache_locks SET expires_at = :next_expires_at "
                            "WHERE application_id = :application_id AND tenant_id = :tenant_id "
                            "AND namespace = :namespace AND key = :key AND owner = :owner "
                            "AND fencing_token = :fencing_token "
                            "AND expires_at = :current_expires_at"
                        ),
                        {
                            "application_id": namespace.application_id,
                            "tenant_id": namespace.tenant_id,
                            "namespace": namespace.name,
                            "key": key.value,
                            "owner": lease.owner.value,
                            "fencing_token": lease.fencing_token,
                            "current_expires_at": lease.expires_at.isoformat(),
                            "next_expires_at": expires_at.isoformat(),
                        },
                    )
                    if renewed.rowcount == 0:
                        return None
                    return LockLease(
                        owner=lease.owner,
                        fencing_token=lease.fencing_token,
                        expires_at=expires_at,
                    )
            except SQLAlchemyError as error:
                raise _failure(error, stage="cache.database.lock.renew") from None

    def release_lock(
        self,
        namespace: CacheNamespace,
        key: CacheKey,
        lease: LockLease,
        /,
    ) -> bool:
        namespace, key = _validated_operation(namespace, key)
        if not isinstance(lease, LockLease):
            raise TypeError("lock release requires a validated lease")
        with self._lock:
            self._assert_open()
            now = self._now()
            if lease.expires_at <= now:
                return False
            try:
                with self._engine.begin() as connection:
                    released = connection.execute(
                        text(
                            "UPDATE af_cache_locks SET expires_at = :released_at "
                            "WHERE application_id = :application_id "
                            "AND tenant_id = :tenant_id AND namespace = :namespace AND key = :key "
                            "AND owner = :owner AND fencing_token = :fencing_token "
                            "AND expires_at = :expires_at"
                        ),
                        {
                            "application_id": namespace.application_id,
                            "tenant_id": namespace.tenant_id,
                            "namespace": namespace.name,
                            "key": key.value,
                            "owner": lease.owner.value,
                            "fencing_token": lease.fencing_token,
                            "expires_at": lease.expires_at.isoformat(),
                            "released_at": now.isoformat(),
                        },
                    )
                    return released.rowcount > 0
            except SQLAlchemyError as error:
                raise _failure(error, stage="cache.database.lock.release") from None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._engine.dispose()
            self._closed = True


def _evidence_digest(
    binding: CapabilityBinding,
    capabilities: CapabilitySet,
    observed_at: datetime,
) -> str:
    payload = {
        "artifact": binding.artifact_digest,
        "capabilities": tuple(capabilities),
        "configuration": binding.configuration_digest,
        "dependencies": binding.dependency_graph_digest,
        "observed_at": observed_at.isoformat(),
        "policy": binding.policy_version,
        "profile": binding.deployment_profile,
        "version": binding.driver_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def database_cache_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[CacheDriver]:
    """Return an inert database-cache declaration with runtime evidence."""

    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="database",
        version=sqlite3.sqlite_version,
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> CacheDriver:
        url = config.get("url")
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "database"
            or set(config) != {"driver", "url"}
            or config.get("driver") != "database"
            or type(url) is not str
            or not url
        ):
            raise ValueError("database cache connection configuration is invalid")
        return DatabaseCacheDriver(url=url, clock=evidence_clock)

    def verify(
        ref: ConnectionRef,
        driver: CacheDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "database"
            or type(driver) is not DatabaseCacheDriver
        ):
            raise ValueError("database cache runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("database cache evidence clock must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        return CapabilityEvidence(
            binding=expected,
            capabilities=_CAPABILITIES,
            health=HealthStatus.HEALTHY,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=1),
            evidence_digest=_evidence_digest(expected, _CAPABILITIES, observed_at),
        )

    return DriverRegistration(
        manager_id="cache",
        driver_name="database",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
