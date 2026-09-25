from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from af.cache.contracts import CacheKey, CacheNamespace, LockLease, LockOwner
from af.cache.database import DatabaseCacheDriver, database_cache_registration
from af.cache.manager import CacheManager
from af.framework.errors import PlatformError, PlatformReason

_NOW = datetime(2026, 8, 1, 11, tzinfo=UTC)


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


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


def _fail_set(
    cache: DatabaseCacheDriver,
    namespace: CacheNamespace,
    key: CacheKey,
    owner: LockOwner,
    lease: LockLease,
) -> None:
    del owner, lease
    cache.set(namespace, key, b"value")


def _fail_delete(
    cache: DatabaseCacheDriver,
    namespace: CacheNamespace,
    key: CacheKey,
    owner: LockOwner,
    lease: LockLease,
) -> bool:
    del owner, lease
    return cache.delete(namespace, key)


def _fail_flush(
    cache: DatabaseCacheDriver,
    namespace: CacheNamespace,
    key: CacheKey,
    owner: LockOwner,
    lease: LockLease,
) -> int:
    del key, owner, lease
    return cache.flush(namespace)


def _fail_acquire_lock(
    cache: DatabaseCacheDriver,
    namespace: CacheNamespace,
    key: CacheKey,
    owner: LockOwner,
    lease: LockLease,
) -> LockLease | None:
    del lease
    return cache.acquire_lock(namespace, key, owner, ttl=timedelta(seconds=1))


def _fail_renew_lock(
    cache: DatabaseCacheDriver,
    namespace: CacheNamespace,
    key: CacheKey,
    owner: LockOwner,
    lease: LockLease,
) -> LockLease | None:
    del owner
    return cache.renew_lock(namespace, key, lease, ttl=timedelta(seconds=1))


def _fail_release_lock(
    cache: DatabaseCacheDriver,
    namespace: CacheNamespace,
    key: CacheKey,
    owner: LockOwner,
    lease: LockLease,
) -> bool:
    del owner
    return cache.release_lock(namespace, key, lease)


def test_database_cache_persists_bytes_across_driver_recreation(tmp_path: Path) -> None:
    clock = Clock()
    namespace = _namespace()
    key = CacheKey("resource:alpha")
    url = _url(tmp_path / "cache.sqlite3")

    first = DatabaseCacheDriver(url=url, clock=clock)
    first.set(namespace, key, b"first")
    first.set(namespace, key, b"replacement")
    first.close()

    second = DatabaseCacheDriver(url=url, clock=clock)
    assert second.get(namespace, key) == b"replacement"
    second.close()


def test_database_cache_honours_exact_expiry_and_discards_invalid_rows(tmp_path: Path) -> None:
    clock = Clock()
    namespace = _namespace()
    key = CacheKey("resource:expiring")
    path = tmp_path / "cache.sqlite3"
    cache = DatabaseCacheDriver(url=_url(path), clock=clock)

    cache.set(namespace, key, b"value", ttl=timedelta(seconds=5))
    clock.now += timedelta(seconds=5)
    assert cache.get(namespace, key) is None

    cache.set(namespace, key, b"value")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE af_cache_entries SET value = ? "
            "WHERE application_id = ? AND tenant_id = ? "
            "AND namespace = ? AND key = ?",
            (b"", namespace.application_id, namespace.tenant_id, namespace.name, key.value),
        )
        connection.commit()

    assert cache.get(namespace, key) is None
    with sqlite3.connect(path) as connection:
        remaining = connection.execute("SELECT COUNT(*) FROM af_cache_entries").fetchone()
    assert remaining == (0,)
    cache.close()


def test_database_cache_read_failures_keep_the_get_failure_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenEngine:
        def begin(self) -> object:
            raise SQLAlchemyError("simulated read failure")

        def dispose(self) -> None:
            pass

    cache = DatabaseCacheDriver(url=_url(tmp_path / "cache.sqlite3"), clock=Clock())
    monkeypatch.setattr(cache, "_engine", BrokenEngine())

    with pytest.raises(PlatformError) as captured:
        cache.get(_namespace(), CacheKey("resource"))

    assert captured.value.stage == "cache.database.get"
    cache.close()


def test_database_cache_flush_is_exactly_namespace_and_tenant_scoped(tmp_path: Path) -> None:
    cache = DatabaseCacheDriver(url=_url(tmp_path / "cache.sqlite3"), clock=Clock())
    tenant_a = _namespace("tenant-a")
    tenant_b = _namespace("tenant-b")
    key = CacheKey("shared")

    cache.set(tenant_a, key, b"a")
    cache.set(tenant_b, key, b"b")

    assert cache.flush(tenant_a) == 1
    assert cache.get(tenant_a, key) is None
    assert cache.get(tenant_b, key) == b"b"
    cache.close()


def test_database_lock_is_owner_fenced_across_driver_instances(tmp_path: Path) -> None:
    clock = Clock()
    url = _url(tmp_path / "cache.sqlite3")
    first = DatabaseCacheDriver(url=url, clock=clock)
    second = DatabaseCacheDriver(url=url, clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:resource")
    first_owner = LockOwner("owner-a")
    second_owner = LockOwner("owner-b")

    first_lease = first.acquire_lock(
        namespace,
        key,
        first_owner,
        ttl=timedelta(seconds=5),
    )
    assert first_lease is not None
    assert (
        second.acquire_lock(
            namespace,
            key,
            second_owner,
            ttl=timedelta(seconds=5),
        )
        is None
    )

    forged = first_lease.__class__(
        owner=second_owner,
        fencing_token=first_lease.fencing_token,
        expires_at=first_lease.expires_at,
    )
    assert (
        second.renew_lock(
            namespace,
            key,
            forged,
            ttl=timedelta(seconds=10),
        )
        is None
    )
    assert not second.release_lock(namespace, key, forged)

    renewed = second.renew_lock(
        namespace,
        key,
        first_lease,
        ttl=timedelta(seconds=10),
    )
    assert renewed is not None
    assert renewed.fencing_token == first_lease.fencing_token

    clock.now += timedelta(seconds=11)
    second_lease = second.acquire_lock(
        namespace,
        key,
        second_owner,
        ttl=timedelta(seconds=5),
    )
    assert second_lease is not None
    assert second_lease.fencing_token > first_lease.fencing_token
    assert not first.release_lock(namespace, key, first_lease)
    assert second.release_lock(namespace, key, second_lease)
    first.close()
    second.close()


def test_database_lock_release_preserves_fencing_progression(tmp_path: Path) -> None:
    clock = Clock()
    cache = DatabaseCacheDriver(url=_url(tmp_path / "cache.sqlite3"), clock=clock)
    namespace = _namespace()
    key = CacheKey("lock:release-reacquire")

    first = cache.acquire_lock(
        namespace,
        key,
        LockOwner("owner-a"),
        ttl=timedelta(seconds=5),
    )
    assert first is not None
    assert cache.release_lock(namespace, key, first)

    second = cache.acquire_lock(
        namespace,
        key,
        LockOwner("owner-b"),
        ttl=timedelta(seconds=5),
    )
    assert second is not None
    assert second.fencing_token > first.fencing_token
    cache.close()


def test_database_cache_registration_routes_through_verified_manager(tmp_path: Path) -> None:
    clock = Clock()
    manager = CacheManager(
        connections={
            "persistent": {
                "driver": "database",
                "url": _url(tmp_path / "cache.sqlite3"),
            }
        },
        default="persistent",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(database_cache_registration(clock=clock))
    namespace = _namespace()
    key = CacheKey("resource")
    lock_key = CacheKey("lock:resource")
    owner = LockOwner("owner-a")

    manager.set(namespace, key, b"value", ttl=timedelta(seconds=30))
    assert manager.get(namespace, key) == b"value"
    assert manager.delete(namespace, key)
    assert manager.get(namespace, key) is None

    lease = manager.acquire_lock(
        namespace,
        lock_key,
        owner,
        ttl=timedelta(seconds=5),
    )
    assert lease is not None
    renewed = manager.renew_lock(
        namespace,
        lock_key,
        lease,
        ttl=timedelta(seconds=10),
    )
    assert renewed is not None
    assert manager.release_lock(namespace, lock_key, renewed)
    manager.close()


def test_database_cache_rejects_invalid_runtime_inputs_and_lock_validation(
    tmp_path: Path,
) -> None:
    with pytest.raises(TypeError):
        DatabaseCacheDriver(  # type: ignore[arg-type]
            url=_url(tmp_path / "invalid-clock.sqlite3"),
            clock=object(),
        )

    cache = DatabaseCacheDriver(
        url=_url(tmp_path / "cache.sqlite3"),
        clock=lambda: datetime(2026, 8, 1, 11),
    )
    namespace = _namespace()
    key = CacheKey("resource")

    with pytest.raises(PlatformError) as captured:
        cache.set(namespace, key, b"value", ttl=timedelta(seconds=1))
    assert captured.value.stage == "cache.database.clock"

    with pytest.raises(TypeError):
        cache.flush(object())  # type: ignore[arg-type]

    owner = LockOwner("owner-a")
    with pytest.raises(TypeError):
        cache.acquire_lock(  # type: ignore[arg-type]
            object(),
            key,
            owner,
            ttl=timedelta(seconds=1),
        )
    with pytest.raises(TypeError):
        cache.renew_lock(  # type: ignore[arg-type]
            namespace,
            key,
            object(),
            ttl=timedelta(seconds=1),
        )
    with pytest.raises(TypeError):
        cache.release_lock(namespace, key, object())  # type: ignore[arg-type]
    cache.close()


def test_database_cache_validates_url_and_fails_closed_after_shutdown(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        DatabaseCacheDriver(url="", clock=Clock())

    cache = DatabaseCacheDriver(url=_url(tmp_path / "cache.sqlite3"), clock=Clock())
    namespace = _namespace()
    key = CacheKey("resource")
    cache.close()
    cache.close()

    with pytest.raises(PlatformError) as captured:
        cache.get(namespace, key)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_database_lock_validates_inputs_and_fails_closed_after_shutdown(
    tmp_path: Path,
) -> None:
    clock = Clock()
    cache = DatabaseCacheDriver(url=_url(tmp_path / "cache.sqlite3"), clock=clock)
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

    lease = LockLease(
        owner=owner,
        fencing_token=1,
        expires_at=_NOW + timedelta(seconds=5),
    )
    with pytest.raises(TypeError):
        cache.renew_lock(  # type: ignore[arg-type]
            namespace,
            key,
            object(),
            ttl=timedelta(seconds=1),
        )
    with pytest.raises(ValueError):
        cache.renew_lock(namespace, key, lease, ttl=timedelta(0))
    with pytest.raises(TypeError):
        cache.release_lock(namespace, key, object())  # type: ignore[arg-type]

    lease = cache.acquire_lock(
        namespace,
        key,
        owner,
        ttl=timedelta(seconds=1),
    )
    assert lease is not None
    clock.now += timedelta(seconds=2)
    assert cache.renew_lock(namespace, key, lease, ttl=timedelta(seconds=1)) is None
    assert not cache.release_lock(namespace, key, lease)
    cache.close()

    with pytest.raises(PlatformError) as captured:
        cache.release_lock(namespace, key, lease)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED


@pytest.mark.parametrize(
    ("operation", "stage"),
    (
        (_fail_set, "cache.database.set"),
        (_fail_delete, "cache.database.delete"),
        (_fail_flush, "cache.database.flush"),
        (_fail_acquire_lock, "cache.database.lock.acquire"),
        (_fail_renew_lock, "cache.database.lock.renew"),
        (_fail_release_lock, "cache.database.lock.release"),
    ),
)
def test_database_cache_operation_failures_preserve_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: object,
    stage: str,
) -> None:
    class BrokenEngine:
        def begin(self) -> object:
            raise SQLAlchemyError("simulated database failure")

        def dispose(self) -> None:
            pass

    cache = DatabaseCacheDriver(url=_url(tmp_path / "cache.sqlite3"), clock=Clock())
    monkeypatch.setattr(cache, "_engine", BrokenEngine())
    namespace = _namespace()
    key = CacheKey("resource")
    owner = LockOwner("owner-a")
    lease = LockLease(
        owner=owner,
        fencing_token=1,
        expires_at=_NOW + timedelta(seconds=5),
    )

    with pytest.raises(PlatformError) as captured:
        operation(cache, namespace, key, owner, lease)  # type: ignore[misc]

    assert captured.value.stage == stage
    cache.close()
