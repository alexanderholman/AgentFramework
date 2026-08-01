from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from af.cache.contracts import CacheKey, CacheNamespace
from af.cache.manager import CacheFeature, CacheManager
from af.cache.memory import MemoryCacheDriver, memory_cache_registration
from af.cache.null import null_cache_registration
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import DriverRegistration, ManagerConfigurationError

_NOW = datetime(2026, 8, 1, 9, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = _NOW

    def __call__(self) -> datetime:
        return self.now


def _namespace(tenant_id: str = "tenant-a") -> CacheNamespace:
    return CacheNamespace(
        application_id="af-test",
        tenant_id=tenant_id,
        name="default",
    )


def test_cache_contracts_reject_unbounded_or_ambiguous_identity() -> None:
    with pytest.raises(ValueError):
        CacheNamespace(application_id="bad app", tenant_id="tenant-a", name="default")
    with pytest.raises(ValueError):
        CacheNamespace(application_id="af-test", tenant_id="", name="default")
    with pytest.raises(ValueError):
        CacheKey("bad key")
    with pytest.raises(ValueError):
        CacheKey("x" * 513)


def test_memory_cache_honours_ttl_and_never_returns_expired_values() -> None:
    clock = Clock()
    cache = MemoryCacheDriver(clock=clock)
    namespace = _namespace()
    key = CacheKey("resource:alpha")

    cache.set(namespace, key, b"value", ttl=timedelta(seconds=5))
    assert cache.get(namespace, key) == b"value"

    clock.now += timedelta(seconds=5)
    assert cache.get(namespace, key) is None
    assert not cache.delete(namespace, key)
    cache.close()


def test_memory_cache_delete_and_flush_are_namespace_isolated() -> None:
    cache = MemoryCacheDriver(clock=Clock())
    tenant_a = _namespace("tenant-a")
    tenant_b = _namespace("tenant-b")
    first = CacheKey("shared")
    second = CacheKey("other")

    cache.set(tenant_a, first, b"a")
    cache.set(tenant_a, second, b"a2")
    cache.set(tenant_b, first, b"b")

    assert cache.delete(tenant_a, first)
    assert cache.get(tenant_a, first) is None
    assert cache.get(tenant_b, first) == b"b"
    assert cache.flush(tenant_a) == 1
    assert cache.get(tenant_a, second) is None
    assert cache.get(tenant_b, first) == b"b"
    cache.close()


def test_memory_cache_validates_values_ttl_and_shutdown() -> None:
    cache = MemoryCacheDriver(clock=Clock())
    namespace = _namespace()
    key = CacheKey("resource")

    with pytest.raises(ValueError):
        cache.set(namespace, key, b"")
    with pytest.raises(ValueError):
        cache.set(namespace, key, b"x" * 1_048_577)
    with pytest.raises(ValueError):
        cache.set(namespace, key, b"value", ttl=timedelta(0))

    cache.close()
    cache.close()
    with pytest.raises(PlatformError) as captured:
        cache.get(namespace, key)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_cache_manager_routes_named_memory_and_null_connections() -> None:
    clock = Clock()
    manager = CacheManager(
        connections={
            "primary": {"driver": "memory"},
            "discard": {"driver": "null"},
        },
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_cache_registration(clock=clock))
    manager.install(null_cache_registration(clock=clock))
    namespace = _namespace()
    key = CacheKey("resource")

    manager.set(namespace, key, b"value")
    assert manager.get(namespace, key) == b"value"
    assert manager.get(namespace, key, name="discard") is None
    manager.set(namespace, key, b"discarded", name="discard")
    assert manager.get(namespace, key, name="discard") is None
    assert manager.connection(requires=(CacheFeature.TTL,)) is manager.connection()
    assert manager.flush(namespace) == 1
    manager.close()


def test_cache_manager_rejects_registration_for_another_manager() -> None:
    clock = Clock()
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    registration = memory_cache_registration(clock=clock)
    untargeted = DriverRegistration(
        manager_id="search",
        driver_name=registration.driver_name,
        factory=registration.factory,
        manifest=registration.manifest,
        verifier=registration.verifier,
        disposer=registration.disposer,
    )

    with pytest.raises(ManagerConfigurationError):
        manager.install(untargeted)
    manager.close()
