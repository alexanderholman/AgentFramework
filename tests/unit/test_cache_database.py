from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from af.cache.contracts import CacheKey, CacheNamespace
from af.cache.database import DatabaseCacheDriver, database_cache_registration
from af.cache.manager import CacheFeature, CacheManager
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

    manager.set(namespace, key, b"value", ttl=timedelta(seconds=30))
    assert manager.get(namespace, key) == b"value"
    assert manager.delete(namespace, key)
    assert manager.get(namespace, key) is None
    with pytest.raises(PlatformError) as captured:
        manager.connection(requires=(CacheFeature.LOCKS,))
    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    manager.close()


def test_database_cache_rejects_invalid_runtime_inputs_and_lock_calls(
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
    with pytest.raises(NotImplementedError):
        cache.acquire_lock()
    with pytest.raises(NotImplementedError):
        cache.renew_lock()
    with pytest.raises(NotImplementedError):
        cache.release_lock()
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
