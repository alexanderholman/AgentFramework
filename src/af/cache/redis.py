"""Verified Redis-backed cache driver with namespace-safe key routing."""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import platform
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Protocol, cast

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

__all__ = ("RedisCacheDriver", "redis_cache_registration")

_CAPABILITIES = CapabilitySet(
    (
        CacheFeature.READ.value,
        CacheFeature.WRITE.value,
        CacheFeature.TTL.value,
        CacheFeature.NAMESPACES.value,
        CacheFeature.LOCKS.value,
    )
)
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_MAX_KEY_BYTES = 1024
_RENEW_SCRIPT = (
    "if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end "
    "redis.call('PSETEX', KEYS[1], ARGV[3], ARGV[2]) return 1"
)
_RELEASE_SCRIPT = (
    "if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end return redis.call('DEL', KEYS[1])"
)


class RedisClient(Protocol):
    def get(self, key: bytes) -> object | None: ...

    def set(
        self,
        key: bytes,
        value: bytes,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool: ...

    def delete(self, *keys: bytes) -> int: ...

    def incr(self, key: bytes, amount: int = 1) -> int: ...

    def eval(self, script: str, numkeys: int, *args: object) -> object: ...

    def close(self) -> None: ...


def _failure(error: BaseException, *, stage: str) -> PlatformError:
    return PlatformError(
        PlatformReason.PERMANENT_FAILURE,
        stage=stage,
        protected_cause=error,
    )


def _runtime_identity() -> tuple[str, str, str]:
    spec = importlib.util.find_spec("redis")
    if spec is None or spec.origin is None:
        raise ValueError("Redis registration requires the redis runtime")
    try:
        redis_version = importlib.metadata.version("redis")
        runtime_bytes = Path(spec.origin).read_bytes()
        driver_bytes = Path(__file__).read_bytes()
    except (OSError, importlib.metadata.PackageNotFoundError) as error:
        raise ValueError("Redis runtime identity is unavailable") from error
    artifact_digest = hashlib.sha256(driver_bytes + b"\0" + runtime_bytes).hexdigest()
    dependency_digest = hashlib.sha256(
        (
            f"{platform.python_implementation()}:{platform.python_version()}:redis={redis_version}"
        ).encode()
    ).hexdigest()
    return redis_version, artifact_digest, dependency_digest


def _host(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 253
        or value != value.strip()
        or _UNSAFE_TEXT.search(value) is not None
        or value not in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError("Redis host is invalid")
    return value


def _port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65_535:
        raise ValueError("Redis port is invalid")
    return value


def _db(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 15:
        raise ValueError("Redis database is invalid")
    return value


def _timeout(value: object, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= 60.0
    ):
        raise ValueError(f"Redis {label} is invalid")
    return float(value)


def _client(
    host: str,
    port: int,
    *,
    db: int,
    socket_connect_timeout: float,
    socket_timeout: float,
) -> RedisClient:
    module = importlib.import_module("redis")
    constructor = vars(module).get("Redis")
    if not callable(constructor):
        raise ValueError("Redis client runtime is invalid")
    client = constructor(
        host=host,
        port=port,
        db=db,
        socket_connect_timeout=socket_connect_timeout,
        socket_timeout=socket_timeout,
        decode_responses=False,
    )
    return cast(RedisClient, client)


class RedisCacheDriver:
    """Tenant-safe Redis cache using namespace-scoped physical keys."""

    def __init__(
        self,
        *,
        client: RedisClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("redis cache clock must be callable")
        self._closed = False

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="cache.redis.clock",
            )
        return value.astimezone(UTC)

    def _assert_open(self) -> None:
        if self._closed:
            raise PlatformError(
                PlatformReason.SHUTDOWN_FAILED,
                stage="cache.redis.connection",
            )

    @staticmethod
    def _namespace_identity(namespace: CacheNamespace) -> str:
        return hashlib.sha256(
            f"{namespace.application_id}\0{namespace.tenant_id}\0{namespace.name}".encode()
        ).hexdigest()

    @staticmethod
    def _key_identity(key: CacheKey) -> str:
        return hashlib.sha256(key.value.encode()).hexdigest()

    @staticmethod
    def _checked_key(value: str) -> bytes:
        encoded = value.encode("ascii")
        if len(encoded) > _MAX_KEY_BYTES:
            raise ValueError("Redis key is invalid")
        return encoded

    def _namespace_version_key(self, namespace: CacheNamespace) -> bytes:
        return self._checked_key(f"af:cache:ns:{self._namespace_identity(namespace)}:v")

    def _namespace_version(self, namespace: CacheNamespace) -> int:
        raw = self._client.get(self._namespace_version_key(namespace))
        if raw is None:
            return 0
        if type(raw) is bytes:
            try:
                value = int(raw.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                raise ValueError("Redis namespace version is invalid") from None
            if value < 0:
                raise ValueError("Redis namespace version is invalid")
            return value
        raise ValueError("Redis namespace version is invalid")

    def _entry_key(self, namespace: CacheNamespace, key: CacheKey) -> bytes:
        return self._checked_key(
            f"af:cache:{self._namespace_identity(namespace)}:{self._namespace_version(namespace)}:"
            f"{self._key_identity(key)}"
        )

    def _lock_key(self, namespace: CacheNamespace, key: CacheKey) -> bytes:
        return self._checked_key(
            f"af:lock:{self._namespace_identity(namespace)}:{self._key_identity(key)}"
        )

    def _lock_token_key(self, namespace: CacheNamespace, key: CacheKey) -> bytes:
        return self._checked_key(
            f"af:lock:{self._namespace_identity(namespace)}:{self._key_identity(key)}:token"
        )

    def _lease_bytes(self, lease: LockLease) -> bytes:
        return json.dumps(
            {
                "owner": lease.owner.value,
                "fencing_token": lease.fencing_token,
                "expires_at": lease.expires_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @staticmethod
    def _lease(value: object) -> LockLease:
        if type(value) is not bytes:
            raise ValueError("Redis lock entry is invalid")
        try:
            payload = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Redis lock entry is invalid") from error
        if type(payload) is not dict:
            raise ValueError("Redis lock entry is invalid")
        owner = payload.get("owner")
        fencing_token = payload.get("fencing_token")
        expires_at = payload.get("expires_at")
        if type(owner) is not str or type(fencing_token) is not int or type(expires_at) is not str:
            raise ValueError("Redis lock entry is invalid")
        try:
            expiry = datetime.fromisoformat(expires_at)
        except ValueError as error:
            raise ValueError("Redis lock entry is invalid") from error
        if expiry.utcoffset() is None:
            raise ValueError("Redis lock entry is invalid")
        return LockLease(
            owner=LockOwner(owner),
            fencing_token=fencing_token,
            expires_at=expiry.astimezone(UTC),
        )

    @staticmethod
    def _ttl_ms(ttl: timedelta | None) -> int | None:
        if ttl is None:
            return None
        return ceil(ttl.total_seconds() * 1000)

    def _next_fencing_token(self, namespace: CacheNamespace, key: CacheKey) -> int:
        try:
            token = self._client.incr(self._lock_token_key(namespace, key), 1)
        except Exception as error:
            raise _failure(error, stage="cache.redis.lock.acquire") from None
        if type(token) is not int or token <= 0:
            cause = RuntimeError("Redis lock token allocation failed")
            raise _failure(cause, stage="cache.redis.lock.acquire")
        return token

    def get(self, namespace: CacheNamespace, key: CacheKey, /) -> bytes | None:
        namespace, key = _validated_operation(namespace, key)
        self._assert_open()
        try:
            value = self._client.get(self._entry_key(namespace, key))
        except Exception as error:
            raise _failure(error, stage="cache.redis.get") from None
        if value is None:
            return None
        return _validated_value(value)

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
        self._assert_open()
        try:
            stored = self._client.set(
                self._entry_key(namespace, key),
                value,
                px=self._ttl_ms(ttl),
            )
        except Exception as error:
            raise _failure(error, stage="cache.redis.set") from None
        if stored is not True:
            cause = RuntimeError("Redis write was not acknowledged")
            raise _failure(cause, stage="cache.redis.set")

    def delete(self, namespace: CacheNamespace, key: CacheKey, /) -> bool:
        namespace, key = _validated_operation(namespace, key)
        self._assert_open()
        try:
            deleted = self._client.delete(self._entry_key(namespace, key))
        except Exception as error:
            raise _failure(error, stage="cache.redis.delete") from None
        return deleted > 0

    def flush(self, namespace: CacheNamespace, /) -> int:
        del namespace
        raise NotImplementedError("redis cache does not advertise flush capability")

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
        self._assert_open()
        try:
            now = self._now()
            ttl_ms = self._ttl_ms(validated_ttl)
            if ttl_ms is None:
                raise ValueError("lock TTL is required")
            token = self._next_fencing_token(namespace, key)
            lease = LockLease(owner=owner, fencing_token=token, expires_at=now + validated_ttl)
            lock_key = self._lock_key(namespace, key)
            encoded = self._lease_bytes(lease)
            created = self._client.set(lock_key, encoded, px=ttl_ms, nx=True)
            if created is True:
                return lease
            current_value = self._client.get(lock_key)
            if current_value is None:
                retried = self._client.set(lock_key, encoded, px=ttl_ms, nx=True)
                return lease if retried is True else None
            current = self._lease(current_value)
            if current.expires_at > now:
                return None
            replaced = self._client.eval(_RENEW_SCRIPT, 1, lock_key, current_value, encoded, ttl_ms)
            return lease if replaced == 1 else None
        except Exception as error:
            raise _failure(error, stage="cache.redis.lock.acquire") from None

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
        self._assert_open()
        try:
            now = self._now()
            if lease.expires_at <= now:
                return None
            ttl_ms = self._ttl_ms(validated_ttl)
            if ttl_ms is None:
                raise ValueError("lock TTL is required")
            renewed = LockLease(
                owner=lease.owner,
                fencing_token=lease.fencing_token,
                expires_at=now + validated_ttl,
            )
            updated = self._client.eval(
                _RENEW_SCRIPT,
                1,
                self._lock_key(namespace, key),
                self._lease_bytes(lease),
                self._lease_bytes(renewed),
                ttl_ms,
            )
        except Exception as error:
            raise _failure(error, stage="cache.redis.lock.renew") from None
        return renewed if updated == 1 else None

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
        self._assert_open()
        try:
            if lease.expires_at <= self._now():
                return False
            deleted = self._client.eval(
                _RELEASE_SCRIPT,
                1,
                self._lock_key(namespace, key),
                self._lease_bytes(lease),
            )
        except Exception as error:
            raise _failure(error, stage="cache.redis.lock.release") from None
        return deleted == 1

    def close(self) -> None:
        if self._closed:
            return
        self._client.close()
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


def redis_cache_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[CacheDriver]:
    """Return an inert Redis cache declaration with independent runtime evidence."""

    version, artifact_digest, dependency_digest = _runtime_identity()
    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="redis",
        version=version,
        artifact_digest=artifact_digest,
        dependency_graph_digest=dependency_digest,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> CacheDriver:
        if ref.manager_id != "cache" or ref.driver_name != "redis":
            raise ValueError("Redis registration received an invalid connection identity")
        allowed = {"driver", "host", "port", "db", "connect_timeout", "timeout"}
        if (
            set(config) - allowed
            or not {"driver", "host", "port", "db"} <= set(config)
            or config.get("driver") != "redis"
        ):
            raise ValueError("Redis connection configuration is invalid")
        client = _client(
            _host(config.get("host")),
            _port(config.get("port")),
            db=_db(config.get("db")),
            socket_connect_timeout=_timeout(
                config.get("connect_timeout", 0.5),
                label="connect timeout",
            ),
            socket_timeout=_timeout(config.get("timeout", 1.0), label="timeout"),
        )
        return RedisCacheDriver(client=client, clock=evidence_clock)

    def verify(
        ref: ConnectionRef,
        driver: CacheDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "redis"
            or type(driver) is not RedisCacheDriver
        ):
            raise ValueError("Redis cache runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("Redis evidence clock must be timezone-aware")
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
        driver_name="redis",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
