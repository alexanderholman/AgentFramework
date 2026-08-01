from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from af.cache import redis as redis_module
from af.cache.contracts import CacheKey, CacheNamespace, LockOwner
from af.cache.manager import CacheDriver, CacheFeature, CacheManager
from af.cache.redis import RedisCacheDriver, redis_cache_registration
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityValidationError,
    ConnectionRef,
    DriverRegistration,
)

_NOW = datetime(2026, 8, 1, 13, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now


class RecordingRedisClient:
    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._entries: dict[bytes, tuple[bytes, datetime | None]] = {}
        self.set_calls: list[dict[str, object]] = []
        self.closed = False

    def _expiry(self, *, ex: int | None, px: int | None) -> datetime | None:
        if ex is not None:
            return self._clock() + timedelta(seconds=ex)
        if px is not None:
            return self._clock() + timedelta(milliseconds=px)
        return None

    def _read(self, key: bytes) -> bytes | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and expires_at <= self._clock():
            self._entries.pop(key, None)
            return None
        return value

    def _store(self, key: bytes, value: bytes, *, ex: int | None, px: int | None) -> None:
        self._entries[key] = (value, self._expiry(ex=ex, px=px))

    def get(self, key: bytes) -> object | None:
        return self._read(key)

    def set(
        self,
        key: bytes,
        value: bytes,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool:
        self.set_calls.append(
            {
                "key": key,
                "value": value,
                "ex": ex,
                "px": px,
                "nx": nx,
                "xx": xx,
            }
        )
        current = self._read(key)
        if nx and current is not None:
            return False
        if xx and current is None:
            return False
        self._store(key, value, ex=ex, px=px)
        return True

    def delete(self, *keys: bytes) -> int:
        deleted = 0
        for key in keys:
            if key in self._entries:
                self._entries.pop(key, None)
                deleted += 1
        return deleted

    def incr(self, key: bytes, amount: int = 1) -> int:
        current = self._read(key)
        value = amount if current is None else int(current.decode("ascii")) + amount
        self._store(key, str(value).encode("ascii"), ex=None, px=None)
        return value

    def eval(self, script: str, numkeys: int, *args: object) -> object:
        assert numkeys == 1
        if script == redis_module._RENEW_SCRIPT:
            key, expected, replacement, ttl_ms = args
            current = self._read(cast(bytes, key))
            if current != expected:
                return 0
            self._store(cast(bytes, key), cast(bytes, replacement), ex=None, px=int(ttl_ms))
            return 1
        if script == redis_module._RELEASE_SCRIPT:
            key, expected = args
            current = self._read(cast(bytes, key))
            if current != expected:
                return 0
            return self.delete(cast(bytes, key))
        raise AssertionError(f"unexpected script: {script}")

    def close(self) -> None:
        self.closed = True

    def evict(self, key: bytes) -> None:
        self._entries.pop(key, None)

    def stored_keys(self) -> tuple[bytes, ...]:
        return tuple(self._entries)


class BrokenRedisClient(RecordingRedisClient):
    def __init__(self, *, clock: Clock, stage: str, namespace_version: object = b"0") -> None:
        super().__init__(clock=clock)
        self._stage = stage
        self._namespace_version = namespace_version

    def get(self, key: bytes) -> object | None:
        if key.endswith(b":v"):
            return self._namespace_version
        if self._stage == "get":
            raise RuntimeError("simulated redis get failure")
        return super().get(key)

    def set(
        self,
        key: bytes,
        value: bytes,
        *,
        ex: int | None = None,
        px: int | None = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool:
        del key, value, ex, px, nx, xx
        if self._stage == "set":
            raise RuntimeError("simulated redis set failure")
        return self._stage != "nack"

    def delete(self, *keys: bytes) -> int:
        del keys
        if self._stage == "delete":
            raise RuntimeError("simulated redis delete failure")
        return 1

    def incr(self, key: bytes, amount: int = 1) -> int:
        del key, amount
        if self._stage == "incr":
            raise RuntimeError("simulated redis incr failure")
        return super().incr(b"token", 1)

    def eval(self, script: str, numkeys: int, *args: object) -> object:
        del script, numkeys, args
        if self._stage == "eval":
            raise RuntimeError("simulated redis eval failure")
        return 0


def _namespace(tenant_id: str = "tenant-a") -> CacheNamespace:
    return CacheNamespace(
        application_id="af-test",
        tenant_id=tenant_id,
        name="default",
    )


def _registration(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DriverRegistration[CacheDriver], Clock, list[RecordingRedisClient]]:
    clock = Clock()
    clients: list[RecordingRedisClient] = []

    def build_client(
        host: str,
        port: int,
        *,
        db: int,
        socket_connect_timeout: float,
        socket_timeout: float,
    ) -> RecordingRedisClient:
        del host, port, db, socket_connect_timeout, socket_timeout
        client = RecordingRedisClient(clock=clock)
        clients.append(client)
        return client

    monkeypatch.setattr(
        redis_module,
        "_runtime_identity",
        lambda: ("6.2.0", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(redis_module, "_client", build_client)
    return redis_cache_registration(clock=clock), clock, clients


def test_redis_runtime_identity_binds_runtime_and_artifact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin="/runtime/redis/__init__.py"),
    )
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: {"redis": "6.2.0"}[name],
    )
    monkeypatch.setattr(Path, "read_bytes", lambda path: str(path).encode())

    version, artifact_digest, dependency_digest = redis_module._runtime_identity()

    assert version == "6.2.0"
    assert len(artifact_digest) == 64
    assert len(dependency_digest) == 64
    assert artifact_digest != dependency_digest


def test_redis_runtime_identity_fails_closed_when_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(ValueError, match="requires the redis runtime"):
        redis_module._runtime_identity()


def test_redis_cache_scopes_keys_by_namespace_and_treats_eviction_as_normal_miss() -> None:
    clock = Clock()
    client = RecordingRedisClient(clock=clock)
    cache = RedisCacheDriver(client=client, clock=clock)
    tenant_a = _namespace("tenant-a")
    tenant_b = _namespace("tenant-b")
    key = CacheKey("shared")

    cache.set(tenant_a, key, b"a")
    cache.set(tenant_b, key, b"b")

    tenant_a_entry = client.stored_keys()[0]
    client.evict(tenant_a_entry)

    assert cache.get(tenant_a, key) is None
    assert cache.get(tenant_b, key) == b"b"
    assert not cache.delete(tenant_a, key)
    cache.close()


def test_redis_cache_uses_millisecond_ttls_and_fails_closed_after_shutdown() -> None:
    clock = Clock()
    client = RecordingRedisClient(clock=clock)
    cache = RedisCacheDriver(client=client, clock=clock)
    namespace = _namespace()

    forever_key = CacheKey("forever")
    ttl_key = CacheKey("ttl")

    cache.set(namespace, forever_key, b"forever")
    assert client.set_calls[-1]["px"] is None

    cache.set(namespace, ttl_key, b"value", ttl=timedelta(seconds=2.1))
    assert client.set_calls[-1]["px"] == 2100
    clock.now += timedelta(milliseconds=2100)
    assert cache.get(namespace, ttl_key) is None

    with pytest.raises(ValueError):
        cache.set(namespace, CacheKey("invalid"), b"", ttl=timedelta(seconds=1))
    with pytest.raises(ValueError):
        cache.set(namespace, CacheKey("invalid-ttl"), b"value", ttl=timedelta(0))

    cache.close()
    with pytest.raises(PlatformError) as captured:
        cache.get(namespace, forever_key)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_redis_lock_is_owner_fenced_and_renews_only_current_lease() -> None:
    clock = Clock()
    client = RecordingRedisClient(clock=clock)
    cache = RedisCacheDriver(client=client, clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:resource")
    first_owner = LockOwner("owner-a")
    second_owner = LockOwner("owner-b")

    first = cache.acquire_lock(namespace, key, first_owner, ttl=timedelta(seconds=5))
    assert first is not None
    assert cache.acquire_lock(namespace, key, second_owner, ttl=timedelta(seconds=5)) is None

    forged = first.__class__(
        owner=second_owner,
        fencing_token=first.fencing_token,
        expires_at=first.expires_at,
    )
    assert cache.renew_lock(namespace, key, forged, ttl=timedelta(seconds=10)) is None
    assert not cache.release_lock(namespace, key, forged)

    renewed = cache.renew_lock(namespace, key, first, ttl=timedelta(seconds=10))
    assert renewed is not None
    assert renewed.fencing_token == first.fencing_token
    clock.now += timedelta(seconds=11)

    second = cache.acquire_lock(namespace, key, second_owner, ttl=timedelta(seconds=5))
    assert second is not None
    assert second.fencing_token > first.fencing_token
    assert not cache.release_lock(namespace, key, first)
    assert cache.release_lock(namespace, key, second)
    cache.close()


def test_redis_lock_handles_expired_missing_and_unclaimed_paths() -> None:
    clock = Clock()
    client = RecordingRedisClient(clock=clock)
    cache = RedisCacheDriver(client=client, clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:resource")
    owner = LockOwner("owner-a")

    first = cache.acquire_lock(namespace, key, owner, ttl=timedelta(seconds=1))
    assert first is not None
    clock.now += timedelta(seconds=2)

    assert cache.renew_lock(namespace, key, first, ttl=timedelta(seconds=1)) is None
    assert not cache.release_lock(namespace, key, first)

    recovered = cache.acquire_lock(namespace, key, owner, ttl=timedelta(seconds=5))
    assert recovered is not None
    assert recovered.fencing_token > first.fencing_token

    client.evict(cache._lock_key(namespace, key))
    assert cache.renew_lock(namespace, key, recovered, ttl=timedelta(seconds=1)) is None
    assert not cache.release_lock(namespace, key, recovered)
    cache.close()


def test_redis_lock_validates_inputs_and_fails_closed_after_shutdown() -> None:
    clock = Clock()
    client = RecordingRedisClient(clock=clock)
    cache = RedisCacheDriver(client=client, clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:resource")
    owner = LockOwner("owner-a")

    with pytest.raises(TypeError):
        cache.acquire_lock(namespace, key, object(), ttl=timedelta(seconds=1))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        cache.acquire_lock(namespace, key, owner, ttl=timedelta(0))

    lease = cache.acquire_lock(namespace, key, owner, ttl=timedelta(seconds=1))
    assert lease is not None
    cache.close()
    with pytest.raises(PlatformError) as captured:
        cache.release_lock(namespace, key, lease)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_redis_cache_covers_error_boundaries_and_optional_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    key = CacheKey("resource")
    lock_key = CacheKey("lock:resource")
    owner = LockOwner("owner-a")

    with pytest.raises(TypeError):
        RedisCacheDriver(
            client=RecordingRedisClient(clock=Clock()),
            clock=cast(Callable[[], datetime], object()),
        )

    naive_cache = RedisCacheDriver(
        client=RecordingRedisClient(clock=Clock()),
        clock=lambda: datetime(2026, 8, 1, 13),
    )
    with pytest.raises(PlatformError) as captured:
        naive_cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=1))
    assert captured.value.stage == "cache.redis.lock.acquire"

    for stage, method_name, expected_stage in (
        ("get", "get", "cache.redis.get"),
        ("set", "set", "cache.redis.set"),
        ("delete", "delete", "cache.redis.delete"),
        ("nack", "set", "cache.redis.set"),
    ):
        cache = RedisCacheDriver(
            client=BrokenRedisClient(clock=Clock(), stage=stage), clock=Clock()
        )
        operation = getattr(cache, method_name)
        with pytest.raises(PlatformError) as stage_error:
            if method_name == "get":
                operation(namespace, key)
            elif method_name == "set":
                operation(namespace, key, b"value", ttl=timedelta(seconds=1))
            else:
                operation(namespace, key)
        assert stage_error.value.stage == expected_stage

    bad_version_cache = RedisCacheDriver(
        client=BrokenRedisClient(clock=Clock(), stage="ok", namespace_version=b"bad"),
        clock=Clock(),
    )
    with pytest.raises(PlatformError) as bad_version:
        bad_version_cache.get(namespace, key)
    assert bad_version.value.stage == "cache.redis.get"

    long_key_cache = RedisCacheDriver(client=RecordingRedisClient(clock=Clock()), clock=Clock())
    monkeypatch.setattr(
        RedisCacheDriver,
        "_namespace_identity",
        staticmethod(lambda _namespace: "x" * 10_000),
    )
    with pytest.raises(PlatformError) as invalid_key:
        long_key_cache.get(namespace, key)
    assert invalid_key.value.stage == "cache.redis.get"
    monkeypatch.setattr(
        RedisCacheDriver,
        "_namespace_identity",
        staticmethod(
            lambda namespace: hashlib.sha256(
                f"{namespace.application_id}\0{namespace.tenant_id}\0{namespace.name}".encode()
            ).hexdigest()
        ),
    )

    unsupported = RedisCacheDriver(client=RecordingRedisClient(clock=Clock()), clock=Clock())
    with pytest.raises(NotImplementedError):
        unsupported.flush(namespace)
    unsupported.close()
    unsupported.close()

    for stage, operation, expected_stage in (
        (
            "incr",
            lambda cache: cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=1)),
            "cache.redis.lock.acquire",
        ),
        (
            "set",
            lambda cache: cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=1)),
            "cache.redis.lock.acquire",
        ),
        (
            "eval",
            lambda cache: cache.renew_lock(
                namespace,
                lock_key,
                cast(
                    object, cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=5))
                ),
                ttl=timedelta(seconds=1),
            ),
            "cache.redis.lock.renew",
        ),
        (
            "eval",
            lambda cache: cache.release_lock(
                namespace,
                lock_key,
                cast(
                    object, cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=5))
                ),
            ),
            "cache.redis.lock.release",
        ),
    ):
        cache = RedisCacheDriver(
            client=BrokenRedisClient(clock=Clock(), stage=stage), clock=Clock()
        )
        with pytest.raises(PlatformError) as stage_error:
            operation(cache)
        assert stage_error.value.stage == expected_stage


def test_redis_registration_is_inert_and_routes_through_verified_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, clock, clients = _registration(monkeypatch)
    ref = ConnectionRef("cache", "shared", "redis")
    driver = registration.factory(
        ref,
        {
            "driver": "redis",
            "host": "127.0.0.1",
            "port": 6379,
            "db": 0,
            "connect_timeout": 0.5,
            "timeout": 1.0,
        },
    )
    binding = CapabilityBinding(
        driver_version="6.2.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest=hashlib.sha256(b"config").hexdigest(),
        deployment_profile="integration",
        policy_version="policy-v1",
    )

    evidence = registration.verifier(ref, driver, binding)

    assert evidence.binding == binding
    assert evidence.observed_at == _NOW
    assert evidence.capabilities.supports(
        {
            CacheFeature.READ.value,
            CacheFeature.WRITE.value,
            CacheFeature.TTL.value,
            CacheFeature.NAMESPACES.value,
            CacheFeature.LOCKS.value,
        }
    )
    assert not evidence.capabilities.supports({CacheFeature.FLUSH.value})
    disposer = registration.disposer
    assert disposer is not None
    disposer(driver)

    manager = CacheManager(
        connections={
            "shared": {
                "driver": "redis",
                "host": "127.0.0.1",
                "port": 6379,
                "db": 0,
            }
        },
        default="shared",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(registration)
    namespace = _namespace()
    key = CacheKey("resource")
    lock_key = CacheKey("lock:resource")
    owner = LockOwner("owner-a")

    manager.set(namespace, key, b"value", ttl=timedelta(seconds=5))
    assert manager.get(namespace, key) == b"value"
    assert manager.delete(namespace, key)
    assert manager.get(namespace, key) is None
    lease = manager.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=5))
    assert lease is not None
    renewed = manager.renew_lock(namespace, lock_key, lease, ttl=timedelta(seconds=10))
    assert renewed is not None
    assert manager.release_lock(namespace, lock_key, renewed)
    with pytest.raises(CapabilityValidationError):
        manager.flush(namespace)

    manager.close()
    assert clients
    assert all(client.closed for client in clients)


def test_redis_registration_rejects_invalid_identity_and_requires_aware_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, _, _ = _registration(monkeypatch)
    ref = ConnectionRef("cache", "shared", "redis")
    with pytest.raises(ValueError, match="invalid connection identity"):
        registration.factory(
            ConnectionRef("search", "shared", "redis"),
            {"driver": "redis", "host": "127.0.0.1", "port": 6379, "db": 0},
        )

    with pytest.raises(ValueError, match="runtime capability verification failed"):
        registration.verifier(
            ref,
            cast(CacheDriver, object()),
            CapabilityBinding(
                driver_version="6.2.0",
                artifact_digest="a" * 64,
                dependency_graph_digest="b" * 64,
                configuration_digest=hashlib.sha256(b"config").hexdigest(),
                deployment_profile="integration",
                policy_version="policy-v1",
            ),
        )

    client = RecordingRedisClient(clock=Clock())
    monkeypatch.setattr(redis_module, "_runtime_identity", lambda: ("6.2.0", "a" * 64, "b" * 64))
    monkeypatch.setattr(
        redis_module,
        "_client",
        lambda host, port, *, db, socket_connect_timeout, socket_timeout: client,
    )
    naive = redis_cache_registration(clock=lambda: datetime(2026, 8, 1, 13))
    driver = naive.factory(
        ref,
        {"driver": "redis", "host": "127.0.0.1", "port": 6379, "db": 0},
    )
    with pytest.raises(ValueError, match="evidence clock must be timezone-aware"):
        naive.verifier(
            ref,
            driver,
            CapabilityBinding(
                driver_version="6.2.0",
                artifact_digest="a" * 64,
                dependency_graph_digest="b" * 64,
                configuration_digest=hashlib.sha256(b"config").hexdigest(),
                deployment_profile="integration",
                policy_version="policy-v1",
            ),
        )
    driver.close()


def test_redis_runtime_identity_and_client_fail_closed_on_invalid_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin="/runtime/redis/__init__.py"),
    )

    def missing_version(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("redis")

    monkeypatch.setattr(importlib.metadata, "version", missing_version)
    with pytest.raises(ValueError, match="runtime identity is unavailable"):
        redis_module._runtime_identity()

    monkeypatch.setattr(importlib, "import_module", lambda _name: SimpleNamespace(Redis=None))
    with pytest.raises(ValueError, match="client runtime is invalid"):
        redis_module._client(
            "127.0.0.1",
            6379,
            db=0,
            socket_connect_timeout=0.5,
            socket_timeout=1.0,
        )

    recorded: dict[str, object] = {}

    class _ClientFactory:
        def __call__(self, **kwargs: object) -> object:
            recorded.update(kwargs)
            return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(
        importlib, "import_module", lambda _name: SimpleNamespace(Redis=_ClientFactory())
    )
    client = redis_module._client(
        "127.0.0.1",
        6379,
        db=0,
        socket_connect_timeout=0.5,
        socket_timeout=1.0,
    )
    assert recorded == {
        "host": "127.0.0.1",
        "port": 6379,
        "db": 0,
        "socket_connect_timeout": 0.5,
        "socket_timeout": 1.0,
        "decode_responses": False,
    }
    assert hasattr(client, "close")


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"driver": "redis", "host": "127.0.0.1"},
        {"driver": "memory", "host": "127.0.0.1", "port": 6379, "db": 0},
        {"driver": "redis", "host": "cache.internal", "port": 6379, "db": 0},
        {"driver": "redis", "host": "127.0.0.1", "port": 0, "db": 0},
        {"driver": "redis", "host": "127.0.0.1", "port": True, "db": 0},
        {"driver": "redis", "host": "127.0.0.1", "port": 6379, "db": True},
        {"driver": "redis", "host": "127.0.0.1", "port": 6379, "db": 0, "timeout": True},
        {
            "driver": "redis",
            "host": "127.0.0.1",
            "port": 6379,
            "db": 0,
            "unknown": "private-secret",
        },
    ],
)
def test_redis_registration_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> None:
    registration, _, _ = _registration(monkeypatch)
    ref = ConnectionRef("cache", "shared", "redis")

    with pytest.raises(ValueError):
        registration.factory(ref, config)
