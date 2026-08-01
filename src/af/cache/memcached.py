"""Verified Memcached-backed cache driver with namespace-safe key versioning."""

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

__all__ = ("MemcachedCacheDriver", "memcached_cache_registration")

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
_MAX_KEY_BYTES = 250
_MEMCACHED_RELATIVE_TTL_LIMIT = 2_592_000


class MemcachedClient(Protocol):
    def get(self, key: bytes, default: object | None = None) -> object | None: ...

    def gets(
        self,
        key: bytes,
        default: object | None = None,
        cas_default: object | None = None,
    ) -> tuple[object | None, object | None]: ...

    def set(
        self,
        key: bytes,
        value: bytes,
        expire: int = 0,
        noreply: bool = False,
    ) -> bool: ...

    def add(
        self,
        key: bytes,
        value: bytes,
        expire: int = 0,
        noreply: bool = False,
        flags: int | None = None,
    ) -> bool: ...

    def cas(
        self,
        key: bytes,
        value: bytes,
        cas: object,
        expire: int = 0,
        noreply: bool = False,
        flags: int | None = None,
    ) -> bool | None: ...

    def incr(self, key: bytes, value: int, noreply: bool = False) -> int | None: ...

    def delete(self, key: bytes, noreply: bool = False) -> bool: ...

    def close(self) -> None: ...


def _failure(error: BaseException, *, stage: str) -> PlatformError:
    return PlatformError(
        PlatformReason.PERMANENT_FAILURE,
        stage=stage,
        protected_cause=error,
    )


def _runtime_identity() -> tuple[str, str, str]:
    spec = importlib.util.find_spec("pymemcache")
    if spec is None or spec.origin is None:
        raise ValueError("Memcached registration requires the pymemcache runtime")
    try:
        pymemcache_version = importlib.metadata.version("pymemcache")
        runtime_bytes = Path(spec.origin).read_bytes()
        driver_bytes = Path(__file__).read_bytes()
    except (OSError, importlib.metadata.PackageNotFoundError) as error:
        raise ValueError("Memcached runtime identity is unavailable") from error
    artifact_digest = hashlib.sha256(driver_bytes + b"\0" + runtime_bytes).hexdigest()
    dependency_digest = hashlib.sha256(
        (
            f"{platform.python_implementation()}:{platform.python_version()}:"
            f"pymemcache={pymemcache_version}"
        ).encode()
    ).hexdigest()
    return pymemcache_version, artifact_digest, dependency_digest


def _host(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 253
        or value != value.strip()
        or _UNSAFE_TEXT.search(value) is not None
        or value not in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError("Memcached host is invalid")
    return value


def _port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 65_535:
        raise ValueError("Memcached port is invalid")
    return value


def _timeout(value: object, *, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 < float(value) <= 60.0
    ):
        raise ValueError(f"Memcached {label} is invalid")
    return float(value)


def _client(
    host: str,
    port: int,
    *,
    connect_timeout: float,
    timeout: float,
) -> MemcachedClient:
    module = importlib.import_module("pymemcache.client.base")
    constructor = vars(module).get("Client")
    if not callable(constructor):
        raise ValueError("Memcached client runtime is invalid")
    client = constructor(
        (host, port),
        connect_timeout=connect_timeout,
        timeout=timeout,
        no_delay=True,
    )
    return cast(MemcachedClient, client)


class MemcachedCacheDriver:
    """Tenant-safe Memcached cache using versioned namespace key prefixes."""

    def __init__(
        self,
        *,
        client: MemcachedClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("memcached cache clock must be callable")
        self._closed = False

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="cache.memcached.clock",
            )
        return value.astimezone(UTC)

    def _assert_open(self) -> None:
        if self._closed:
            raise PlatformError(
                PlatformReason.SHUTDOWN_FAILED,
                stage="cache.memcached.connection",
            )

    @staticmethod
    def _namespace_identity(namespace: CacheNamespace) -> str:
        return hashlib.sha256(
            f"{namespace.application_id}\0{namespace.tenant_id}\0{namespace.name}".encode()
        ).hexdigest()

    @staticmethod
    def _key_identity(key: CacheKey) -> str:
        return hashlib.sha256(key.value.encode()).hexdigest()

    def _namespace_version_key(self, namespace: CacheNamespace) -> bytes:
        return f"af:cache:ns:{self._namespace_identity(namespace)}:v".encode("ascii")

    def _namespace_version(self, namespace: CacheNamespace) -> int:
        raw = self._client.get(self._namespace_version_key(namespace))
        if raw is None:
            return 0
        if type(raw) is bytes:
            try:
                value = int(raw.decode("ascii"))
            except (UnicodeDecodeError, ValueError):
                raise ValueError("Memcached namespace version is invalid") from None
            if value < 0:
                raise ValueError("Memcached namespace version is invalid")
            return value
        raise ValueError("Memcached namespace version is invalid")

    def _entry_key(self, namespace: CacheNamespace, key: CacheKey) -> bytes:
        version = self._namespace_version(namespace)
        encoded = (
            f"af:cache:{self._namespace_identity(namespace)}:{version}:{self._key_identity(key)}"
        ).encode("ascii")
        if len(encoded) > _MAX_KEY_BYTES:
            raise ValueError("Memcached key is invalid")
        return encoded

    def _lock_key(self, namespace: CacheNamespace, key: CacheKey) -> bytes:
        encoded = (
            f"af:lock:{self._namespace_identity(namespace)}:{self._key_identity(key)}"
        ).encode("ascii")
        if len(encoded) > _MAX_KEY_BYTES:
            raise ValueError("Memcached key is invalid")
        return encoded

    def _lock_token_key(self, namespace: CacheNamespace, key: CacheKey) -> bytes:
        encoded = (
            f"af:lock:{self._namespace_identity(namespace)}:{self._key_identity(key)}:token"
        ).encode("ascii")
        if len(encoded) > _MAX_KEY_BYTES:
            raise ValueError("Memcached key is invalid")
        return encoded

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
            raise ValueError("Memcached lock entry is invalid")
        try:
            payload = json.loads(value.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Memcached lock entry is invalid") from error
        if type(payload) is not dict:
            raise ValueError("Memcached lock entry is invalid")
        owner = payload.get("owner")
        fencing_token = payload.get("fencing_token")
        expires_at = payload.get("expires_at")
        if type(owner) is not str or type(fencing_token) is not int or type(expires_at) is not str:
            raise ValueError("Memcached lock entry is invalid")
        try:
            expiry = datetime.fromisoformat(expires_at)
        except ValueError as error:
            raise ValueError("Memcached lock entry is invalid") from error
        if expiry.utcoffset() is None:
            raise ValueError("Memcached lock entry is invalid")
        return LockLease(
            owner=LockOwner(owner),
            fencing_token=fencing_token,
            expires_at=expiry.astimezone(UTC),
        )

    def _next_fencing_token(self, namespace: CacheNamespace, key: CacheKey) -> int:
        try:
            token_key = self._lock_token_key(namespace, key)
            self._client.add(token_key, b"0", expire=0, noreply=False)
            token = self._client.incr(token_key, 1, noreply=False)
        except Exception as error:
            raise _failure(error, stage="cache.memcached.lock.acquire") from None
        if type(token) is not int or token <= 0:
            cause = RuntimeError("Memcached lock token allocation failed")
            raise _failure(cause, stage="cache.memcached.lock.acquire")
        return token

    def _expire(self, ttl: timedelta | None) -> int:
        if ttl is None:
            return 0
        seconds = ceil(ttl.total_seconds())
        if seconds <= _MEMCACHED_RELATIVE_TTL_LIMIT:
            return seconds
        return int((self._now() + ttl).timestamp())

    def get(self, namespace: CacheNamespace, key: CacheKey, /) -> bytes | None:
        namespace, key = _validated_operation(namespace, key)
        self._assert_open()
        try:
            value = self._client.get(self._entry_key(namespace, key))
        except Exception as error:
            raise _failure(error, stage="cache.memcached.get") from None
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
                expire=self._expire(ttl),
                noreply=False,
            )
        except Exception as error:
            raise _failure(error, stage="cache.memcached.set") from None
        if stored is not True:
            cause = RuntimeError("Memcached write was not acknowledged")
            raise _failure(cause, stage="cache.memcached.set")

    def delete(self, namespace: CacheNamespace, key: CacheKey, /) -> bool:
        namespace, key = _validated_operation(namespace, key)
        self._assert_open()
        try:
            deleted = self._client.delete(self._entry_key(namespace, key), noreply=False)
        except Exception as error:
            raise _failure(error, stage="cache.memcached.delete") from None
        return bool(deleted)

    def flush(self, namespace: CacheNamespace, /) -> int:
        del namespace
        raise NotImplementedError("memcached cache does not advertise flush capability")

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
        now = self._now()
        expires_at = now + validated_ttl
        token = self._next_fencing_token(namespace, key)
        lease = LockLease(owner=owner, fencing_token=token, expires_at=expires_at)
        try:
            lock_key = self._lock_key(namespace, key)
            created = self._client.add(
                lock_key,
                self._lease_bytes(lease),
                expire=self._expire(validated_ttl),
                noreply=False,
            )
        except Exception as error:
            raise _failure(error, stage="cache.memcached.lock.acquire") from None
        if created is True:
            return lease
        try:
            current_value, cas_token = self._client.gets(lock_key, default=None, cas_default=None)
        except Exception as error:
            raise _failure(error, stage="cache.memcached.lock.acquire") from None
        if current_value is None or cas_token is None:
            try:
                added = self._client.add(
                    lock_key,
                    self._lease_bytes(lease),
                    expire=self._expire(validated_ttl),
                    noreply=False,
                )
            except Exception as error:
                raise _failure(error, stage="cache.memcached.lock.acquire") from None
            return lease if added is True else None
        current = self._lease(current_value)
        if current.expires_at > now:
            return None
        replaced: bool | None
        try:
            replaced = self._client.cas(
                lock_key,
                self._lease_bytes(lease),
                cas_token,
                expire=self._expire(validated_ttl),
                noreply=False,
            )
        except Exception as error:
            raise _failure(error, stage="cache.memcached.lock.acquire") from None
        return lease if replaced is True else None

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
        now = self._now()
        if lease.expires_at <= now:
            return None
        try:
            lock_key = self._lock_key(namespace, key)
            current_value, cas_token = self._client.gets(lock_key, default=None, cas_default=None)
        except Exception as error:
            raise _failure(error, stage="cache.memcached.lock.renew") from None
        if current_value is None or cas_token is None:
            return None
        try:
            current = self._lease(current_value)
        except ValueError as error:
            raise _failure(error, stage="cache.memcached.lock.renew") from None
        if current != lease or current.expires_at <= now:
            return None
        renewed = LockLease(
            owner=lease.owner,
            fencing_token=lease.fencing_token,
            expires_at=now + validated_ttl,
        )
        try:
            updated = self._client.cas(
                lock_key,
                self._lease_bytes(renewed),
                cas_token,
                expire=self._expire(validated_ttl),
                noreply=False,
            )
        except Exception as error:
            raise _failure(error, stage="cache.memcached.lock.renew") from None
        return renewed if updated is True else None

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
        now = self._now()
        if lease.expires_at <= now:
            return False
        try:
            lock_key = self._lock_key(namespace, key)
            current_value, cas_token = self._client.gets(lock_key, default=None, cas_default=None)
        except Exception as error:
            raise _failure(error, stage="cache.memcached.lock.release") from None
        if current_value is None or cas_token is None:
            return False
        try:
            current = self._lease(current_value)
        except ValueError as error:
            raise _failure(error, stage="cache.memcached.lock.release") from None
        if current != lease or current.expires_at <= now:
            return False
        release_ttl = timedelta(minutes=1)
        release_marker = LockLease(
            owner=lease.owner,
            fencing_token=lease.fencing_token,
            expires_at=now + release_ttl,
        )
        try:
            claimed = self._client.cas(
                lock_key,
                self._lease_bytes(release_marker),
                cas_token,
                expire=self._expire(release_ttl),
                noreply=False,
            )
            if claimed is not True:
                return False
            self._client.delete(lock_key, noreply=False)
        except Exception as error:
            raise _failure(error, stage="cache.memcached.lock.release") from None
        return True

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


def memcached_cache_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[CacheDriver]:
    """Return an inert Memcached cache declaration with independent runtime evidence."""

    version, artifact_digest, dependency_digest = _runtime_identity()
    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="memcached",
        version=version,
        artifact_digest=artifact_digest,
        dependency_graph_digest=dependency_digest,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> CacheDriver:
        if ref.manager_id != "cache" or ref.driver_name != "memcached":
            raise ValueError("Memcached registration received an invalid connection identity")
        allowed = {"driver", "host", "port", "connect_timeout", "timeout"}
        if (
            set(config) - allowed
            or not {"driver", "host", "port"} <= set(config)
            or config.get("driver") != "memcached"
        ):
            raise ValueError("Memcached connection configuration is invalid")
        client = _client(
            _host(config.get("host")),
            _port(config.get("port")),
            connect_timeout=_timeout(config.get("connect_timeout", 0.5), label="connect timeout"),
            timeout=_timeout(config.get("timeout", 1.0), label="timeout"),
        )
        return MemcachedCacheDriver(client=client, clock=evidence_clock)

    def verify(
        ref: ConnectionRef,
        driver: CacheDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "cache"
            or ref.driver_name != "memcached"
            or type(driver) is not MemcachedCacheDriver
        ):
            raise ValueError("Memcached cache runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("Memcached evidence clock must be timezone-aware")
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
        driver_name="memcached",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
