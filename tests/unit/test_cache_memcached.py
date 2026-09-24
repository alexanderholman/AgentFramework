from __future__ import annotations

import hashlib
import importlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from af.cache import memcached as memcached_module
from af.cache.contracts import CacheKey, CacheNamespace, LockOwner
from af.cache.manager import CacheDriver, CacheFeature, CacheManager
from af.cache.memcached import MemcachedCacheDriver, memcached_cache_registration
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityValidationError,
    ConnectionRef,
    DriverRegistration,
)

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now


class RecordingMemcachedClient:
    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._entries: dict[bytes, tuple[bytes, datetime | None]] = {}
        self._cas_tokens: dict[bytes, int] = {}
        self._next_cas_token = 1
        self.set_calls: list[dict[str, object]] = []
        self.closed = False

    def _expiry(self, expire: int) -> datetime | None:
        if expire == 0:
            return None
        if expire > 2_592_000:
            return datetime.fromtimestamp(expire, UTC)
        return self._clock() + timedelta(seconds=expire)

    def _read(self, key: bytes) -> bytes | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and expires_at <= self._clock():
            self._entries.pop(key, None)
            self._cas_tokens.pop(key, None)
            return None
        return value

    def _store(self, key: bytes, value: bytes, expire: int) -> None:
        self._entries[key] = (value, self._expiry(expire))
        self._cas_tokens[key] = self._next_cas_token
        self._next_cas_token += 1

    def get(self, key: bytes, default: object | None = None) -> object | None:
        value = self._read(key)
        if value is None:
            return default
        return value

    def gets(
        self,
        key: bytes,
        default: object | None = None,
        cas_default: object | None = None,
    ) -> tuple[object | None, object | None]:
        value = self._read(key)
        if value is None:
            return default, cas_default
        return value, self._cas_tokens[key]

    def set(
        self,
        key: bytes,
        value: bytes,
        expire: int = 0,
        noreply: bool = False,
    ) -> bool:
        self.set_calls.append(
            {
                "key": key,
                "value": value,
                "expire": expire,
                "noreply": noreply,
            }
        )
        self._store(key, value, expire)
        return True

    def add(
        self,
        key: bytes,
        value: bytes,
        expire: int = 0,
        noreply: bool = False,
        flags: object | None = None,
    ) -> bool:
        del noreply, flags
        if self._read(key) is not None:
            return False
        self._store(key, value, expire)
        return True

    def cas(
        self,
        key: bytes,
        value: bytes,
        cas: object,
        expire: int = 0,
        noreply: bool = False,
        flags: object | None = None,
    ) -> bool | None:
        del noreply, flags
        current = self._read(key)
        if current is None:
            return None
        if self._cas_tokens.get(key) != cas:
            return False
        self._store(key, value, expire)
        return True

    def incr(self, key: bytes, value: int, noreply: bool = False) -> int | None:
        del noreply
        current = self._read(key)
        if current is None:
            return None
        next_value = int(current.decode("ascii")) + value
        self._store(key, str(next_value).encode("ascii"), 0)
        return next_value

    def delete(self, key: bytes, noreply: bool = False) -> bool:
        del noreply
        self._cas_tokens.pop(key, None)
        return self._entries.pop(key, None) is not None

    def close(self) -> None:
        self.closed = True

    def evict(self, key: bytes) -> None:
        self._entries.pop(key, None)

    def stored_keys(self) -> tuple[bytes, ...]:
        return tuple(self._entries)


class BrokenMemcachedClient(RecordingMemcachedClient):
    def __init__(self, *, clock: Clock, stage: str, namespace_version: object = b"0") -> None:
        super().__init__(clock=clock)
        self._stage = stage
        self._namespace_version = namespace_version

    def get(self, key: bytes, default: object | None = None) -> object | None:
        if key.endswith(b":v"):
            return self._namespace_version
        if self._stage == "get":
            raise RuntimeError("simulated memcached get failure")
        return super().get(key, default)

    def set(
        self,
        key: bytes,
        value: bytes,
        expire: int = 0,
        noreply: bool = False,
    ) -> bool:
        del key, value, expire, noreply
        if self._stage == "set":
            raise RuntimeError("simulated memcached set failure")
        return self._stage != "nack"

    def gets(
        self,
        key: bytes,
        default: object | None = None,
        cas_default: object | None = None,
    ) -> tuple[object | None, object | None]:
        if self._stage == "gets":
            raise RuntimeError("simulated memcached gets failure")
        return super().gets(key, default, cas_default)

    def add(
        self,
        key: bytes,
        value: bytes,
        expire: int = 0,
        noreply: bool = False,
        flags: object | None = None,
    ) -> bool:
        if self._stage == "add":
            raise RuntimeError("simulated memcached add failure")
        return super().add(key, value, expire, noreply, flags)

    def cas(
        self,
        key: bytes,
        value: bytes,
        cas: object,
        expire: int = 0,
        noreply: bool = False,
        flags: object | None = None,
    ) -> bool | None:
        if self._stage == "cas":
            raise RuntimeError("simulated memcached cas failure")
        return super().cas(key, value, cas, expire, noreply, flags)

    def incr(self, key: bytes, value: int, noreply: bool = False) -> int | None:
        if self._stage == "incr":
            raise RuntimeError("simulated memcached incr failure")
        return super().incr(key, value, noreply)

    def delete(self, key: bytes, noreply: bool = False) -> bool:
        del key, noreply
        if self._stage == "delete":
            raise RuntimeError("simulated memcached delete failure")
        return True


def _namespace(tenant_id: str = "tenant-a") -> CacheNamespace:
    return CacheNamespace(
        application_id="af-test",
        tenant_id=tenant_id,
        name="default",
    )


def _registration(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[DriverRegistration[CacheDriver], Clock, list[RecordingMemcachedClient]]:
    clock = Clock()
    clients: list[RecordingMemcachedClient] = []

    def build_client(
        host: str,
        port: int,
        *,
        connect_timeout: float,
        timeout: float,
    ) -> RecordingMemcachedClient:
        del host, port, connect_timeout, timeout
        client = RecordingMemcachedClient(clock=clock)
        clients.append(client)
        return client

    monkeypatch.setattr(
        memcached_module,
        "_runtime_identity",
        lambda: ("4.0.0", "a" * 64, "b" * 64),
    )
    monkeypatch.setattr(memcached_module, "_client", build_client)
    return memcached_cache_registration(clock=clock), clock, clients


def test_memcached_runtime_identity_binds_runtime_and_artifact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin="/runtime/pymemcache/__init__.py"),
    )
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: {"pymemcache": "4.0.0"}[name],
    )
    monkeypatch.setattr(Path, "read_bytes", lambda path: str(path).encode())

    version, artifact_digest, dependency_digest = memcached_module._runtime_identity()

    assert version == "4.0.0"
    assert len(artifact_digest) == 64
    assert len(dependency_digest) == 64
    assert artifact_digest != dependency_digest


def test_memcached_runtime_identity_fails_closed_when_runtime_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(ValueError, match="requires the pymemcache runtime"):
        memcached_module._runtime_identity()


def test_memcached_cache_scopes_keys_by_namespace_and_treats_eviction_as_normal_miss() -> None:
    clock = Clock()
    client = RecordingMemcachedClient(clock=clock)
    cache = MemcachedCacheDriver(client=client, clock=clock)
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


def test_memcached_cache_uses_memcached_ttl_semantics_and_fails_closed_after_shutdown() -> None:
    clock = Clock()
    client = RecordingMemcachedClient(clock=clock)
    cache = MemcachedCacheDriver(client=client, clock=clock)
    namespace = _namespace()

    forever_key = CacheKey("forever")
    relative_key = CacheKey("relative")
    absolute_key = CacheKey("absolute")

    cache.set(namespace, forever_key, b"forever")
    forever_expire = client.set_calls[-1]["expire"]
    assert forever_expire == 0

    cache.set(namespace, relative_key, b"value", ttl=timedelta(seconds=2.1))
    relative_expire = client.set_calls[-1]["expire"]
    assert relative_expire == 3
    clock.now += timedelta(seconds=3)
    assert cache.get(namespace, relative_key) is None

    clock.now = _NOW
    cache.set(namespace, absolute_key, b"value", ttl=timedelta(days=31))
    assert client.set_calls[-1]["expire"] == int((_NOW + timedelta(days=31)).timestamp())

    with pytest.raises(ValueError):
        cache.set(namespace, CacheKey("invalid"), b"", ttl=timedelta(seconds=1))
    with pytest.raises(ValueError):
        cache.set(namespace, CacheKey("invalid-ttl"), b"value", ttl=timedelta(0))

    cache.close()
    with pytest.raises(PlatformError) as captured:
        cache.get(namespace, forever_key)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_memcached_lock_is_owner_fenced_and_renews_only_current_lease() -> None:
    clock = Clock()
    client = RecordingMemcachedClient(clock=clock)
    cache = MemcachedCacheDriver(client=client, clock=clock)
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


def test_memcached_release_cannot_delete_a_replacement_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    client = RecordingMemcachedClient(clock=clock)
    cache = MemcachedCacheDriver(client=client, clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:release-race")
    first = cache.acquire_lock(
        namespace,
        key,
        LockOwner("owner-a"),
        ttl=timedelta(seconds=5),
    )
    assert first is not None
    original_cas = client.cas
    contenders: list[object] = []

    def acquire_after_release_marker(
        lock_key: bytes,
        value: bytes,
        cas_token: object,
        expire: int = 0,
        noreply: bool = False,
        flags: object | None = None,
    ) -> bool | None:
        result = original_cas(lock_key, value, cas_token, expire, noreply, flags)
        if result is True and lock_key == cache._lock_key(namespace, key):
            marker = cache._lease(value)
            if marker.expires_at <= clock():
                # Simulate a process pause after Memcached accepts the release CAS.
                clock.now += timedelta(minutes=2)
                contenders.append(
                    cache.acquire_lock(
                        namespace,
                        key,
                        LockOwner("owner-b"),
                        ttl=timedelta(seconds=5),
                    )
                )
        return result

    monkeypatch.setattr(client, "cas", acquire_after_release_marker)

    assert cache.release_lock(namespace, key, first)
    replacement_lease = contenders[0]
    assert replacement_lease is not None
    assert not cache.release_lock(namespace, key, first)
    assert (
        cache.acquire_lock(
            namespace,
            key,
            LockOwner("owner-c"),
            ttl=timedelta(seconds=5),
        )
        is None
    )
    cache.close()


def test_memcached_lock_handles_expired_missing_and_unclaimed_paths() -> None:
    clock = Clock()
    client = RecordingMemcachedClient(clock=clock)
    cache = MemcachedCacheDriver(client=client, clock=clock)
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


def test_memcached_lock_validates_inputs_and_invalid_entries_fail_closed() -> None:
    clock = Clock()
    client = RecordingMemcachedClient(clock=clock)
    cache = MemcachedCacheDriver(client=client, clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:resource")
    owner = LockOwner("owner-a")
    lease = cache.acquire_lock(namespace, key, owner, ttl=timedelta(seconds=5))
    assert lease is not None

    with pytest.raises(TypeError):
        cache.acquire_lock(namespace, key, object(), ttl=timedelta(seconds=1))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        cache.acquire_lock(namespace, key, owner, ttl=timedelta(0))
    with pytest.raises(TypeError):
        cache.renew_lock(namespace, key, object(), ttl=timedelta(seconds=1))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        cache.renew_lock(namespace, key, lease, ttl=timedelta(0))
    with pytest.raises(TypeError):
        cache.release_lock(namespace, key, object())  # type: ignore[arg-type]

    client._entries[cache._lock_key(namespace, key)] = (b"not-json", clock() + timedelta(seconds=5))
    with pytest.raises(PlatformError) as renew_error:
        cache.renew_lock(namespace, key, lease, ttl=timedelta(seconds=1))
    assert renew_error.value.stage == "cache.memcached.lock.renew"
    with pytest.raises(PlatformError) as release_error:
        cache.release_lock(namespace, key, lease)
    assert release_error.value.stage == "cache.memcached.lock.release"
    cache.close()


def test_memcached_lock_handles_missing_tokens_and_compare_exchange_misses() -> None:
    clock = Clock()
    client = RecordingMemcachedClient(clock=clock)
    cache = MemcachedCacheDriver(client=client, clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:resource")
    owner = LockOwner("owner-a")

    token_key = cache._lock_token_key(namespace, key)
    client._entries[token_key] = (b"not-a-number", None)
    with pytest.raises(PlatformError) as token_error:
        cache.acquire_lock(namespace, key, owner, ttl=timedelta(seconds=5))
    assert token_error.value.stage == "cache.memcached.lock.acquire"

    repaired = RecordingMemcachedClient(clock=clock)
    cache = MemcachedCacheDriver(client=repaired, clock=clock)
    lease = cache.acquire_lock(namespace, key, owner, ttl=timedelta(seconds=1))
    assert lease is not None
    clock.now += timedelta(seconds=2)

    lock_key = cache._lock_key(namespace, key)
    stale_value = repaired._entries[lock_key][0]
    stale_cas = repaired._cas_tokens[lock_key]
    replacement = cache.acquire_lock(namespace, key, LockOwner("owner-b"), ttl=timedelta(seconds=5))
    assert replacement is not None
    assert replacement.owner == LockOwner("owner-b")

    assert repaired.cas(lock_key, stale_value, stale_cas, expire=1, noreply=False) is False
    cache.close()


def test_memcached_cache_covers_error_boundaries_and_optional_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    key = CacheKey("resource")
    lock_key = CacheKey("lock:resource")
    owner = LockOwner("owner-a")

    with pytest.raises(TypeError):
        MemcachedCacheDriver(
            client=RecordingMemcachedClient(clock=Clock()),
            clock=cast(Callable[[], datetime], object()),
        )

    naive_cache = MemcachedCacheDriver(
        client=RecordingMemcachedClient(clock=Clock()),
        clock=lambda: datetime(2026, 8, 1, 12),
    )
    with pytest.raises(PlatformError) as captured:
        naive_cache.set(namespace, key, b"value", ttl=timedelta(days=31))
    assert captured.value.stage == "cache.memcached.set"

    for stage, method_name, expected_stage in (
        ("get", "get", "cache.memcached.get"),
        ("set", "set", "cache.memcached.set"),
        ("delete", "delete", "cache.memcached.delete"),
        ("nack", "set", "cache.memcached.set"),
    ):
        cache = MemcachedCacheDriver(
            client=BrokenMemcachedClient(clock=Clock(), stage=stage), clock=Clock()
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

    bad_version_cache = MemcachedCacheDriver(
        client=BrokenMemcachedClient(clock=Clock(), stage="ok", namespace_version=b"bad"),
        clock=Clock(),
    )
    with pytest.raises(PlatformError) as bad_version:
        bad_version_cache.get(namespace, key)
    assert bad_version.value.stage == "cache.memcached.get"

    long_key_cache = MemcachedCacheDriver(
        client=RecordingMemcachedClient(clock=Clock()), clock=Clock()
    )
    monkeypatch.setattr(
        MemcachedCacheDriver,
        "_namespace_identity",
        staticmethod(lambda _namespace: "x" * 240),
    )
    with pytest.raises(PlatformError) as invalid_key:
        long_key_cache.get(namespace, key)
    assert invalid_key.value.stage == "cache.memcached.get"
    monkeypatch.setattr(
        MemcachedCacheDriver,
        "_namespace_identity",
        staticmethod(
            lambda namespace: hashlib.sha256(
                f"{namespace.application_id}\0{namespace.tenant_id}\0{namespace.name}".encode()
            ).hexdigest()
        ),
    )

    unsupported = MemcachedCacheDriver(
        client=RecordingMemcachedClient(clock=Clock()), clock=Clock()
    )
    with pytest.raises(NotImplementedError):
        unsupported.flush(namespace)
    unsupported.close()
    unsupported.close()

    for stage, operation, expected_stage in (
        (
            "add",
            lambda cache: cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=1)),
            "cache.memcached.lock.acquire",
        ),
        (
            "incr",
            lambda cache: cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=1)),
            "cache.memcached.lock.acquire",
        ),
        (
            "gets",
            lambda cache: cache.renew_lock(
                namespace,
                lock_key,
                cast(
                    object, cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=5))
                ),
                ttl=timedelta(seconds=1),
            ),
            "cache.memcached.lock.renew",
        ),
        (
            "cas",
            lambda cache: cache.renew_lock(
                namespace,
                lock_key,
                cast(
                    object, cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=5))
                ),
                ttl=timedelta(seconds=1),
            ),
            "cache.memcached.lock.renew",
        ),
        (
            "delete",
            lambda cache: cache.release_lock(
                namespace,
                lock_key,
                cast(
                    object, cache.acquire_lock(namespace, lock_key, owner, ttl=timedelta(seconds=5))
                ),
            ),
            "cache.memcached.lock.release",
        ),
    ):
        cache = MemcachedCacheDriver(
            client=BrokenMemcachedClient(clock=Clock(), stage=stage),
            clock=Clock(),
        )
        with pytest.raises(PlatformError) as stage_error:
            operation(cache)
        assert stage_error.value.stage == expected_stage


def test_memcached_registration_is_inert_and_routes_through_verified_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, clock, clients = _registration(monkeypatch)
    ref = ConnectionRef("cache", "shared", "memcached")
    driver = registration.factory(
        ref,
        {
            "driver": "memcached",
            "host": "127.0.0.1",
            "port": 11211,
            "connect_timeout": 0.5,
            "timeout": 1.0,
        },
    )
    binding = CapabilityBinding(
        driver_version="4.0.0",
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
                "driver": "memcached",
                "host": "127.0.0.1",
                "port": 11211,
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


def test_memcached_registration_rejects_invalid_identity_and_requires_aware_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registration, _, _ = _registration(monkeypatch)
    ref = ConnectionRef("cache", "shared", "memcached")
    with pytest.raises(ValueError, match="invalid connection identity"):
        registration.factory(
            ConnectionRef("search", "shared", "memcached"),
            {"driver": "memcached", "host": "127.0.0.1", "port": 11211},
        )

    with pytest.raises(ValueError, match="runtime capability verification failed"):
        registration.verifier(
            ref,
            cast(CacheDriver, object()),
            CapabilityBinding(
                driver_version="4.0.0",
                artifact_digest="a" * 64,
                dependency_graph_digest="b" * 64,
                configuration_digest=hashlib.sha256(b"config").hexdigest(),
                deployment_profile="integration",
                policy_version="policy-v1",
            ),
        )

    client = RecordingMemcachedClient(clock=Clock())
    monkeypatch.setattr(
        memcached_module, "_runtime_identity", lambda: ("4.0.0", "a" * 64, "b" * 64)
    )
    monkeypatch.setattr(
        memcached_module, "_client", lambda host, port, *, connect_timeout, timeout: client
    )
    naive = memcached_cache_registration(clock=lambda: datetime(2026, 8, 1, 12))
    driver = naive.factory(
        ref,
        {"driver": "memcached", "host": "127.0.0.1", "port": 11211},
    )
    with pytest.raises(ValueError, match="evidence clock must be timezone-aware"):
        naive.verifier(
            ref,
            driver,
            CapabilityBinding(
                driver_version="4.0.0",
                artifact_digest="a" * 64,
                dependency_graph_digest="b" * 64,
                configuration_digest=hashlib.sha256(b"config").hexdigest(),
                deployment_profile="integration",
                policy_version="policy-v1",
            ),
        )
    driver.close()


def test_memcached_runtime_identity_and_client_fail_closed_on_invalid_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: SimpleNamespace(origin="/runtime/pymemcache/__init__.py"),
    )

    def missing_version(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError("pymemcache")

    monkeypatch.setattr(importlib.metadata, "version", missing_version)
    with pytest.raises(ValueError, match="runtime identity is unavailable"):
        memcached_module._runtime_identity()

    monkeypatch.setattr(importlib, "import_module", lambda _name: SimpleNamespace(Client=None))
    with pytest.raises(ValueError, match="client runtime is invalid"):
        memcached_module._client("127.0.0.1", 11211, connect_timeout=0.5, timeout=1.0)

    recorded: dict[str, object] = {}

    class _ClientFactory:
        def __call__(self, address: tuple[str, int], **kwargs: object) -> object:
            recorded["address"] = address
            recorded["kwargs"] = kwargs
            return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(
        importlib, "import_module", lambda _name: SimpleNamespace(Client=_ClientFactory())
    )
    client = memcached_module._client("127.0.0.1", 11211, connect_timeout=0.5, timeout=1.0)
    assert recorded == {
        "address": ("127.0.0.1", 11211),
        "kwargs": {"connect_timeout": 0.5, "timeout": 1.0, "no_delay": True},
    }
    assert hasattr(client, "close")


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"driver": "memcached", "host": "127.0.0.1"},
        {"driver": "memory", "host": "127.0.0.1", "port": 11211},
        {"driver": "memcached", "host": "cache.internal", "port": 11211},
        {"driver": "memcached", "host": "127.0.0.1", "port": 0},
        {"driver": "memcached", "host": "127.0.0.1", "port": True},
        {"driver": "memcached", "host": "127.0.0.1", "port": 11211, "timeout": True},
        {
            "driver": "memcached",
            "host": "127.0.0.1",
            "port": 11211,
            "unknown": "private-secret",
        },
    ],
)
def test_memcached_registration_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch,
    config: dict[str, object],
) -> None:
    registration, _, _ = _registration(monkeypatch)
    ref = ConnectionRef("cache", "shared", "memcached")

    with pytest.raises(ValueError):
        registration.factory(ref, config)
