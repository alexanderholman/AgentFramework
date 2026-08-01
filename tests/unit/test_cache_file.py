from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from af.cache.contracts import CacheKey, CacheNamespace
from af.cache.file import FileCacheDriver, file_cache_registration
from af.cache.manager import CacheManager
from af.framework.errors import PlatformError, PlatformReason

_NOW = datetime(2026, 8, 1, 10, tzinfo=UTC)


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


def test_file_cache_persists_bytes_across_driver_recreation(tmp_path: Path) -> None:
    clock = Clock()
    namespace = _namespace()
    key = CacheKey("resource:alpha")

    first = FileCacheDriver(root=tmp_path, clock=clock)
    first.set(namespace, key, b"first")
    first.set(namespace, key, b"replacement")
    first.close()

    second = FileCacheDriver(root=tmp_path, clock=clock)
    assert second.get(namespace, key) == b"replacement"
    assert tuple(tmp_path.rglob("*.tmp")) == ()
    second.close()


def test_file_cache_honours_exact_expiry_and_discards_corruption(tmp_path: Path) -> None:
    clock = Clock()
    cache = FileCacheDriver(root=tmp_path, clock=clock)
    namespace = _namespace()
    key = CacheKey("resource:expiring")

    cache.set(namespace, key, b"value", ttl=timedelta(seconds=5))
    clock.now += timedelta(seconds=5)
    assert cache.get(namespace, key) is None

    cache.set(namespace, key, b"value")
    (entry_path,) = tuple(tmp_path.rglob("*.afc"))
    entry_path.write_bytes(b"not-a-cache-frame")
    assert cache.get(namespace, key) is None
    assert not entry_path.exists()
    cache.close()


def test_file_cache_flush_is_exactly_namespace_and_tenant_scoped(tmp_path: Path) -> None:
    cache = FileCacheDriver(root=tmp_path, clock=Clock())
    tenant_a = _namespace("tenant-a")
    tenant_b = _namespace("tenant-b")
    key = CacheKey("shared")

    cache.set(tenant_a, key, b"a")
    cache.set(tenant_b, key, b"b")

    assert cache.flush(tenant_a) == 1
    assert cache.get(tenant_a, key) is None
    assert cache.get(tenant_b, key) == b"b"
    cache.close()


def test_file_cache_registration_routes_through_verified_manager(tmp_path: Path) -> None:
    clock = Clock()
    manager = CacheManager(
        connections={
            "persistent": {
                "driver": "file",
                "path": str(tmp_path),
            }
        },
        default="persistent",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=clock,
    )
    manager.install(file_cache_registration(clock=clock))
    namespace = _namespace()
    key = CacheKey("resource")

    manager.set(namespace, key, b"value", ttl=timedelta(seconds=30))
    assert manager.get(namespace, key) == b"value"
    assert manager.delete(namespace, key)
    assert manager.get(namespace, key) is None
    manager.close()


def test_file_cache_validates_root_and_fails_closed_after_shutdown(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        FileCacheDriver(root=tmp_path / "missing" / "..", clock=Clock())

    cache = FileCacheDriver(root=tmp_path, clock=Clock())
    namespace = _namespace()
    key = CacheKey("resource")
    cache.close()
    cache.close()

    with pytest.raises(PlatformError) as captured:
        cache.get(namespace, key)
    assert captured.value.reason is PlatformReason.SHUTDOWN_FAILED
