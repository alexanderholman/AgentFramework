from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from af.cache.contracts import CacheKey, CacheNamespace, LockLease, LockOwner
from af.cache.manager import CacheFeature, CacheManager, bind_cache_manager
from af.cache.memory import MemoryCacheDriver, memory_cache_registration
from af.cache.null import NullCacheDriver, null_cache_registration
from af.framework.application import Application
from af.framework.container import Container, ProviderRegistrar, ServiceProvider
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    DriverResolutionError,
    ManagerConfigurationError,
)

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


def test_cache_manager_preserves_generic_capability_inputs() -> None:
    clock = Clock()
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_cache_registration(clock=clock))

    driver = manager.connection(requires=CapabilitySet((CacheFeature.READ.value,)))
    assert manager.connection(requires=CacheFeature.WRITE.value) is driver
    with pytest.raises(ValueError):
        manager.connection(requires=(object(),))  # type: ignore[arg-type]
    manager.close()


def test_cache_manager_is_application_owned_and_snapshots_configuration() -> None:
    clock = Clock()
    connections = {"primary": {"driver": "memory"}}

    class CacheProvider(ServiceProvider):
        provider_id = "cache"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_cache_manager(
                registrar,
                connections=connections,
                default="primary",
                deployment_profile="test",
                policy_version="policy-v1",
                registrations=(memory_cache_registration(clock=clock),),
                clock=clock,
            )

    application = Application(Container(), (CacheProvider(),))
    application.register()
    connections["primary"]["driver"] = "null"
    application.boot()
    application.serve()

    manager = application.container.resolve(CacheManager)
    namespace = _namespace()
    key = CacheKey("resource")
    manager.set(namespace, key, b"value")
    assert manager.get(namespace, key) == b"value"

    application.shutdown()
    with pytest.raises(DriverResolutionError) as captured:
        manager.get(namespace, key)
    assert captured.value.reason is PlatformReason.DEPENDENCY_RESOLUTION_FAILED


def test_cache_provider_rejects_missing_or_duplicate_registrations() -> None:
    class Registrar:
        def bind(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del args, kwargs

    clock = Clock()
    registration = memory_cache_registration(clock=clock)
    common = {
        "connections": {"primary": {"driver": "memory"}},
        "default": "primary",
        "deployment_profile": "test",
        "policy_version": "policy-v1",
        "clock": clock,
    }
    with pytest.raises(ManagerConfigurationError):
        bind_cache_manager(
            Registrar(),
            registrations=(),
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ManagerConfigurationError):
        bind_cache_manager(
            Registrar(),  # type: ignore[arg-type]
            registrations=(registration, registration),
            **common,
        )


def test_null_cache_validates_operations_and_fails_closed_after_shutdown() -> None:
    cache = NullCacheDriver()
    namespace = _namespace()
    key = CacheKey("resource")

    with pytest.raises(ValueError):
        cache.set(namespace, key, b"")
    with pytest.raises(ValueError):
        cache.set(namespace, key, b"value", ttl=timedelta(0))
    assert not cache.delete(namespace, key)
    assert cache.flush(namespace) == 0

    cache.close()
    with pytest.raises(PlatformError) as captured:
        cache.get(namespace, key)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_memory_lock_is_owner_fenced_and_renews_only_current_lease() -> None:
    clock = Clock()
    cache = MemoryCacheDriver(clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:resource")
    first_owner = LockOwner("owner-a")
    second_owner = LockOwner("owner-b")

    first = cache.acquire_lock(
        namespace,
        key,
        first_owner,
        ttl=timedelta(seconds=5),
    )
    assert first is not None
    assert (
        cache.acquire_lock(
            namespace,
            key,
            second_owner,
            ttl=timedelta(seconds=5),
        )
        is None
    )

    forged = replace(first, owner=second_owner)
    assert (
        cache.renew_lock(
            namespace,
            key,
            forged,
            ttl=timedelta(seconds=10),
        )
        is None
    )
    assert not cache.release_lock(namespace, key, forged)

    renewed = cache.renew_lock(
        namespace,
        key,
        first,
        ttl=timedelta(seconds=10),
    )
    assert renewed is not None
    assert renewed.fencing_token == first.fencing_token
    clock.now += timedelta(seconds=11)

    second = cache.acquire_lock(
        namespace,
        key,
        second_owner,
        ttl=timedelta(seconds=5),
    )
    assert second is not None
    assert second.fencing_token > first.fencing_token
    assert not cache.release_lock(namespace, key, first)
    assert cache.release_lock(namespace, key, second)
    cache.close()


def test_memory_lock_contention_permits_one_active_owner() -> None:
    cache = MemoryCacheDriver(clock=Clock())
    namespace = _namespace()
    key = CacheKey("lock:contended")
    barrier = Barrier(8)

    def contend(index: int) -> object:
        barrier.wait()
        return cache.acquire_lock(
            namespace,
            key,
            LockOwner(f"owner-{index}"),
            ttl=timedelta(seconds=30),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        leases = tuple(executor.map(contend, range(8)))

    assert sum(lease is not None for lease in leases) == 1
    cache.close()


def test_cache_manager_routes_verified_lock_capability() -> None:
    clock = Clock()
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_cache_registration(clock=clock))
    namespace = _namespace()
    key = CacheKey("lock:manager")
    owner = LockOwner("owner-a")

    lease = manager.acquire_lock(
        namespace,
        key,
        owner,
        ttl=timedelta(seconds=5),
    )
    assert lease is not None
    renewed = manager.renew_lock(
        namespace,
        key,
        lease,
        ttl=timedelta(seconds=10),
    )
    assert renewed is not None
    assert manager.release_lock(namespace, key, renewed)
    manager.close()


def test_cache_manager_remember_populates_cache_once_per_key() -> None:
    clock = Clock()
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_cache_registration(clock=clock))
    namespace = _namespace()
    key = CacheKey("remember:resource")
    calls = 0

    def load() -> bytes:
        nonlocal calls
        calls += 1
        return b"remembered"

    assert manager.remember(namespace, key, load, ttl=timedelta(seconds=30)) == b"remembered"
    assert manager.remember(namespace, key, load, ttl=timedelta(seconds=30)) == b"remembered"
    assert manager.get(namespace, key) == b"remembered"
    assert calls == 1
    manager.close()


def test_cache_manager_memoize_wraps_loader_with_cache_lookup() -> None:
    clock = Clock()
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_cache_registration(clock=clock))
    namespace = _namespace()
    key = CacheKey("memoize:resource")
    calls = 0

    def load() -> bytes:
        nonlocal calls
        calls += 1
        return b"memoized"

    memoized = manager.memoize(namespace, key, load, ttl=timedelta(seconds=30))

    assert memoized() == b"memoized"
    assert memoized() == b"memoized"
    assert manager.get(namespace, key) == b"memoized"
    assert calls == 1
    manager.close()


def test_cache_manager_remember_and_memoize_validate_loader() -> None:
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=Clock(),
    )
    manager.install(memory_cache_registration(clock=Clock()))
    namespace = _namespace()
    key = CacheKey("remember:validated")
    manager.set(namespace, key, b"already-cached")

    with pytest.raises(TypeError):
        manager.remember(namespace, key, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        manager.memoize(namespace, key, object())  # type: ignore[arg-type]
    manager.close()


def test_lock_owner_identity_is_bounded_and_redacted() -> None:
    with pytest.raises(ValueError):
        LockOwner("")
    with pytest.raises(ValueError):
        LockOwner("x" * 513)
    assert "owner-secret" not in repr(LockOwner("owner-secret"))


def test_memory_lock_validates_inputs_and_fails_closed_after_shutdown() -> None:
    cache = MemoryCacheDriver(clock=Clock())
    namespace = _namespace()
    key = CacheKey("lock:validated")
    owner = LockOwner("owner-a")

    with pytest.raises(TypeError):
        cache.acquire_lock(  # type: ignore[arg-type]
            namespace,
            key,
            object(),
            ttl=timedelta(seconds=1),
        )
    with pytest.raises(ValueError):
        cache.acquire_lock(namespace, key, owner, ttl=timedelta(0))

    lease = cache.acquire_lock(
        namespace,
        key,
        owner,
        ttl=timedelta(seconds=1),
    )
    assert lease is not None
    cache.close()
    with pytest.raises(PlatformError) as captured:
        cache.release_lock(namespace, key, lease)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_null_lock_operations_validate_and_never_claim_ownership() -> None:
    cache = NullCacheDriver()
    namespace = _namespace()
    key = CacheKey("lock:null")
    owner = LockOwner("owner-a")
    lease = LockLease(
        owner=owner,
        fencing_token=1,
        expires_at=_NOW + timedelta(seconds=5),
    )

    assert (
        cache.acquire_lock(
            namespace,
            key,
            owner,
            ttl=timedelta(seconds=5),
        )
        is None
    )
    assert (
        cache.renew_lock(
            namespace,
            key,
            lease,
            ttl=timedelta(seconds=5),
        )
        is None
    )
    assert not cache.release_lock(namespace, key, lease)
    cache.close()
    with pytest.raises(PlatformError):
        cache.acquire_lock(
            namespace,
            key,
            owner,
            ttl=timedelta(seconds=5),
        )


def test_lock_lease_rejects_invalid_fence_or_expiry() -> None:
    owner = LockOwner("owner-a")
    with pytest.raises(ValueError):
        LockLease(owner=owner, fencing_token=0, expires_at=_NOW)
    with pytest.raises(ValueError):
        LockLease(
            owner=owner,
            fencing_token=1,
            expires_at=datetime(2026, 8, 1, 9),
        )
