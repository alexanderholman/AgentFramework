from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event, Lock

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


def test_cache_manager_remember_coalesces_contended_loaders() -> None:
    clock = Clock()
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(memory_cache_registration(clock=clock))
    manager.connection()
    namespace = _namespace()
    key = CacheKey("remember:coalesced")
    barrier = Barrier(8)
    loader_started = Event()
    release_loader = Event()
    counter_lock = Lock()
    calls = 0

    def load() -> bytes:
        nonlocal calls
        with counter_lock:
            calls += 1
        loader_started.set()
        assert release_loader.wait(timeout=1)
        return b"coalesced"

    def remember() -> bytes:
        barrier.wait()
        return manager.remember(
            namespace,
            key,
            load,
            ttl=timedelta(seconds=30),
            lock_ttl=timedelta(seconds=5),
            wait_timeout=timedelta(seconds=1),
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = tuple(executor.submit(remember) for _ in range(8))
        assert loader_started.wait(timeout=1)
        release_loader.set()
        values = tuple(future.result(timeout=2) for future in futures)

    assert values == (b"coalesced",) * 8
    assert calls == 1
    manager.close()


def test_cache_manager_remember_fails_boundedly_when_funnel_stays_busy() -> None:
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
    key = CacheKey("remember:busy")
    lease = manager.acquire_lock(
        namespace,
        key,
        LockOwner("existing-owner"),
        ttl=timedelta(seconds=30),
    )
    assert lease is not None

    with pytest.raises(PlatformError) as captured:
        manager.remember(
            namespace,
            key,
            lambda: b"must-not-load",
            lock_ttl=timedelta(seconds=5),
            wait_timeout=timedelta(milliseconds=1),
        )

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert captured.value.stage == "cache.remember.wait_timeout"
    assert manager.release_lock(namespace, key, lease)
    manager.close()


def test_cache_manager_remember_rejects_stale_loader_write_after_lease_loss() -> None:
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
    key = CacheKey("remember:lease-loss")

    def load() -> bytes:
        clock.now += timedelta(seconds=2)
        return b"stale"

    with pytest.raises(PlatformError) as captured:
        manager.remember(
            namespace,
            key,
            load,
            lock_ttl=timedelta(seconds=1),
            wait_timeout=timedelta(seconds=1),
        )

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert captured.value.stage == "cache.remember.lease_lost"
    assert manager.get(namespace, key) is None
    manager.close()


def test_cache_manager_remember_validates_funnel_durations() -> None:
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=Clock(),
    )
    manager.install(memory_cache_registration(clock=Clock()))
    namespace = _namespace()
    key = CacheKey("remember:funnel-validation")

    with pytest.raises(ValueError, match="lock TTL"):
        manager.remember(
            namespace,
            key,
            lambda: b"value",
            lock_ttl=timedelta(0),
        )
    with pytest.raises(ValueError, match="requires a lock TTL"):
        manager.remember(
            namespace,
            key,
            lambda: b"value",
            wait_timeout=timedelta(seconds=1),
        )
    with pytest.raises(ValueError, match="wait timeout"):
        manager.remember(
            namespace,
            key,
            lambda: b"value",
            lock_ttl=timedelta(seconds=1),
            wait_timeout=timedelta(0),
        )
    manager.close()


def test_cache_manager_stale_while_revalidate_serves_stale_and_refreshes_once() -> None:
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
    key = CacheKey("swr:resource")
    scheduled: list[Callable[[], None]] = []
    calls = 0

    def load() -> bytes:
        nonlocal calls
        calls += 1
        return f"value-{calls}".encode()

    def schedule(refresh: Callable[[], None]) -> None:
        scheduled.append(refresh)

    def cached() -> bytes:
        return manager.stale_while_revalidate(
            namespace,
            key,
            load,
            fresh_ttl=timedelta(seconds=5),
            stale_ttl=timedelta(seconds=10),
            lock_ttl=timedelta(seconds=5),
            wait_timeout=timedelta(seconds=1),
            schedule=schedule,
        )

    assert cached() == b"value-1"
    clock.now += timedelta(seconds=5)

    assert cached() == b"value-1"
    assert cached() == b"value-1"
    assert calls == 1
    assert len(scheduled) == 1

    scheduled.pop()()
    assert cached() == b"value-2"
    assert calls == 2
    manager.close()


def test_cache_manager_stale_while_revalidate_preserves_stale_on_refresh_failure() -> None:
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
    key = CacheKey("swr:failure")
    scheduled: list[Callable[[], None]] = []
    failing = False

    def load() -> bytes:
        if failing:
            raise RuntimeError("refresh unavailable")
        return b"stable"

    def cached() -> bytes:
        return manager.stale_while_revalidate(
            namespace,
            key,
            load,
            fresh_ttl=timedelta(seconds=1),
            stale_ttl=timedelta(seconds=10),
            lock_ttl=timedelta(seconds=5),
            wait_timeout=timedelta(seconds=1),
            schedule=scheduled.append,
        )

    assert cached() == b"stable"
    clock.now += timedelta(seconds=1)
    failing = True

    assert cached() == b"stable"
    scheduled.pop()()
    assert manager.get(namespace, key) == b"stable"

    assert cached() == b"stable"
    assert len(scheduled) == 1
    manager.close()


def test_cache_manager_stale_while_revalidate_cold_fills_after_hard_expiry() -> None:
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
    key = CacheKey("swr:hard-expiry")
    calls = 0

    def load() -> bytes:
        nonlocal calls
        calls += 1
        return f"value-{calls}".encode()

    def cached() -> bytes:
        return manager.stale_while_revalidate(
            namespace,
            key,
            load,
            fresh_ttl=timedelta(seconds=2),
            stale_ttl=timedelta(seconds=3),
            lock_ttl=timedelta(seconds=1),
            wait_timeout=timedelta(seconds=1),
            schedule=lambda refresh: None,
        )

    assert cached() == b"value-1"
    clock.now += timedelta(seconds=5)
    assert cached() == b"value-2"
    assert calls == 2
    manager.close()


def test_cache_manager_stale_while_revalidate_validates_policy() -> None:
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=Clock(),
    )
    manager.install(memory_cache_registration(clock=Clock()))
    namespace = _namespace()
    key = CacheKey("swr:validation")
    durations = timedelta(seconds=1)
    for fresh_ttl, stale_ttl, lock_ttl, wait_timeout in (
        (timedelta(0), durations, durations, durations),
        (durations, timedelta(0), durations, durations),
        (durations, durations, timedelta(0), durations),
        (durations, durations, durations, timedelta(0)),
    ):
        with pytest.raises(ValueError):
            manager.stale_while_revalidate(
                namespace,
                key,
                lambda: b"value",
                fresh_ttl=fresh_ttl,
                stale_ttl=stale_ttl,
                lock_ttl=lock_ttl,
                wait_timeout=wait_timeout,
                schedule=lambda refresh: None,
            )
    with pytest.raises(TypeError, match="scheduler"):
        manager.stale_while_revalidate(
            namespace,
            key,
            lambda: b"value",
            fresh_ttl=durations,
            stale_ttl=durations,
            lock_ttl=durations,
            wait_timeout=durations,
            schedule=object(),  # type: ignore[arg-type]
        )
    manager.close()


def test_cache_manager_stale_while_revalidate_releases_lease_when_scheduling_fails() -> None:
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
    key = CacheKey("swr:schedule-failure")
    durations = timedelta(seconds=1)
    scheduled: list[Callable[[], None]] = []

    def reject(_refresh: Callable[[], None]) -> None:
        raise RuntimeError("scheduler unavailable")

    assert (
        manager.stale_while_revalidate(
            namespace,
            key,
            lambda: b"stable",
            fresh_ttl=durations,
            stale_ttl=timedelta(seconds=10),
            lock_ttl=durations,
            wait_timeout=durations,
            schedule=reject,
        )
        == b"stable"
    )
    clock.now += durations
    assert (
        manager.stale_while_revalidate(
            namespace,
            key,
            lambda: b"replacement",
            fresh_ttl=durations,
            stale_ttl=timedelta(seconds=10),
            lock_ttl=durations,
            wait_timeout=durations,
            schedule=reject,
        )
        == b"stable"
    )
    assert (
        manager.stale_while_revalidate(
            namespace,
            key,
            lambda: b"replacement",
            fresh_ttl=durations,
            stale_ttl=timedelta(seconds=10),
            lock_ttl=durations,
            wait_timeout=durations,
            schedule=scheduled.append,
        )
        == b"stable"
    )
    assert len(scheduled) == 1
    manager.close()


def test_cache_manager_stale_while_revalidate_rejects_expired_refresh_lease() -> None:
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
    key = CacheKey("swr:expired-refresh")
    scheduled: list[Callable[[], None]] = []
    durations = timedelta(seconds=1)

    def cached() -> bytes:
        return manager.stale_while_revalidate(
            namespace,
            key,
            lambda: b"replacement",
            fresh_ttl=durations,
            stale_ttl=timedelta(seconds=10),
            lock_ttl=durations,
            wait_timeout=durations,
            schedule=scheduled.append,
        )

    assert cached() == b"replacement"
    manager.set(namespace, key, b"stable", ttl=timedelta(seconds=11))
    clock.now += durations
    assert cached() == b"stable"
    clock.now += durations
    scheduled.pop()()
    assert manager.get(namespace, key) == b"stable"
    assert cached() == b"stable"
    assert len(scheduled) == 1
    manager.close()


def test_cache_manager_stale_while_revalidate_rejects_cold_fill_after_lease_loss() -> None:
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
    key = CacheKey("swr:cold-lease-loss")

    def load() -> bytes:
        clock.now += timedelta(seconds=2)
        return b"stale-loader"

    with pytest.raises(PlatformError) as captured:
        manager.stale_while_revalidate(
            namespace,
            key,
            load,
            fresh_ttl=timedelta(seconds=1),
            stale_ttl=timedelta(seconds=10),
            lock_ttl=timedelta(seconds=1),
            wait_timeout=timedelta(seconds=1),
            schedule=lambda refresh: None,
        )
    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert captured.value.stage == "cache.swr.lease_lost"
    assert manager.get(namespace, key) is None
    manager.close()


def test_cache_manager_stale_while_revalidate_cold_fill_waits_boundedly() -> None:
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
    key = CacheKey("swr:cold-busy")
    lease = manager.acquire_lock(
        namespace,
        key,
        LockOwner("existing-swr-owner"),
        ttl=timedelta(seconds=10),
    )
    assert lease is not None

    with pytest.raises(PlatformError) as captured:
        manager.stale_while_revalidate(
            namespace,
            key,
            lambda: b"must-not-load",
            fresh_ttl=timedelta(seconds=1),
            stale_ttl=timedelta(seconds=10),
            lock_ttl=timedelta(seconds=1),
            wait_timeout=timedelta(milliseconds=1),
            schedule=lambda refresh: None,
        )
    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert captured.value.stage == "cache.swr.wait_timeout"
    assert manager.release_lock(namespace, key, lease)
    manager.close()


def test_cache_manager_stale_while_revalidate_rejects_unbounded_horizon() -> None:
    manager = CacheManager(
        connections={"primary": {"driver": "memory"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=Clock(),
    )
    manager.install(memory_cache_registration(clock=Clock()))

    with pytest.raises(ValueError, match="stale horizon"):
        manager.stale_while_revalidate(
            _namespace(),
            CacheKey("swr:overflow"),
            lambda: b"value",
            fresh_ttl=timedelta.max,
            stale_ttl=timedelta(days=1),
            lock_ttl=timedelta(seconds=1),
            wait_timeout=timedelta(seconds=1),
            schedule=lambda refresh: None,
        )
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

    memoized = manager.memoize(
        namespace,
        key,
        load,
        ttl=timedelta(seconds=30),
        lock_ttl=timedelta(seconds=5),
        wait_timeout=timedelta(seconds=1),
    )

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
