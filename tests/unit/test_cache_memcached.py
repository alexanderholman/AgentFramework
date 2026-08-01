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
from af.cache.contracts import CacheKey, CacheNamespace
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
        self.set_calls: list[dict[str, object]] = []
        self.closed = False

    def _expiry(self, expire: int) -> datetime | None:
        if expire == 0:
            return None
        if expire > 2_592_000:
            return datetime.fromtimestamp(expire, UTC)
        return self._clock() + timedelta(seconds=expire)

    def get(self, key: bytes, default: object | None = None) -> object | None:
        del default
        entry = self._entries.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and expires_at <= self._clock():
            self._entries.pop(key, None)
            return None
        return value

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
        self._entries[key] = (value, self._expiry(expire))
        return True

    def delete(self, key: bytes, noreply: bool = False) -> bool:
        del noreply
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


def test_memcached_cache_covers_error_boundaries_and_optional_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _namespace()
    key = CacheKey("resource")
    owner = object()

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

    unsupported = MemcachedCacheDriver(
        client=RecordingMemcachedClient(clock=Clock()), clock=Clock()
    )
    with pytest.raises(NotImplementedError):
        unsupported.flush(namespace)
    with pytest.raises(NotImplementedError):
        unsupported.acquire_lock(namespace, key, owner, ttl=timedelta(seconds=1))
    with pytest.raises(NotImplementedError):
        unsupported.renew_lock(namespace, key, owner, ttl=timedelta(seconds=1))
    with pytest.raises(NotImplementedError):
        unsupported.release_lock(namespace, key, owner)
    unsupported.close()
    unsupported.close()


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
        }
    )
    assert not evidence.capabilities.supports({CacheFeature.FLUSH.value, CacheFeature.LOCKS.value})
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

    manager.set(namespace, key, b"value", ttl=timedelta(seconds=5))
    assert manager.get(namespace, key) == b"value"
    assert manager.delete(namespace, key)
    assert manager.get(namespace, key) is None
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
