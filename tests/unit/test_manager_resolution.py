from __future__ import annotations

import asyncio
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Lock

import pytest

from af.framework.errors import AsyncResolutionRequired
from af.framework.managers import (
    CapabilitySet,
    ConnectionConfig,
    ConnectionRef,
    DriverCapabilityUnavailable,
    DriverQuarantined,
    HealthState,
    HealthStatus,
    InvalidManagerConfiguration,
    Manager,
    UnknownConnection,
)


@dataclass
class _Driver:
    name: str
    status: HealthStatus
    closed: int = 0

    def health(self) -> HealthStatus:
        return self.status

    def close(self) -> None:
        self.closed += 1


def _healthy(*capabilities: str, revision: int = 0) -> HealthStatus:
    return HealthStatus(
        HealthState.HEALTHY,
        CapabilitySet.of(*capabilities),
        revision=revision,
    )


def test_named_and_default_connections_are_lazy_and_cached() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {
            "primary": ConnectionConfig("memory"),
            "secondary": ConnectionConfig("memory"),
        },
        default="primary",
    )
    calls: list[str] = []

    def build(ref: ConnectionRef, options: Mapping[str, object]) -> _Driver:
        del options
        calls.append(ref.name)
        return _Driver(ref.name, _healthy("cache.read"))

    manager.extend("memory", build)

    assert manager.cached_connections == ()
    assert manager.connection().name == "primary"
    assert manager.connection().name == "primary"
    assert manager.connection("secondary").name == "secondary"
    assert calls == ["primary", "secondary"]
    assert manager.cached_connections == ("primary", "secondary")


def test_required_capabilities_fail_without_implicit_fallback() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {
            "primary": ConnectionConfig(
                "memory",
                required_capabilities=CapabilitySet.of("cache.read"),
            )
        },
        default="primary",
    )
    driver = _Driver("primary", _healthy("cache.read"))
    manager.extend("memory", lambda ref, options: driver)

    with pytest.raises(DriverCapabilityUnavailable) as captured:
        manager.connection(required=CapabilitySet.of("cache.lock"))

    assert captured.value.missing == CapabilitySet.of("cache.lock")
    assert manager.connection() is driver


def test_runtime_capability_regression_quarantines_cached_connection() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )
    first = _Driver("primary", _healthy("cache.read", revision=1))
    second = _Driver("primary-rebuilt", _healthy("cache.read", revision=3))
    built = iter((first, second))
    manager.extend("memory", lambda ref, options: next(built))

    assert manager.connection(required=CapabilitySet.of("cache.read")) is first
    first.status = _healthy(revision=2)

    with pytest.raises(DriverQuarantined) as captured:
        manager.connection()
    assert captured.value.reason_code == "capability.runtime_regression"
    assert manager.quarantine_reason("primary") == "capability.runtime_regression"

    with pytest.raises(DriverQuarantined):
        manager.connection()
    manager.restore("primary")
    assert manager.connection() is second


def test_health_revision_conflicts_fail_closed() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )
    driver = _Driver("primary", _healthy("cache.read", revision=1))
    manager.extend("memory", lambda ref, options: driver)
    manager.connection()

    driver.status = HealthStatus(
        HealthState.DEGRADED,
        CapabilitySet.of("cache.read"),
        reason_code="backend.slow",
        revision=1,
    )
    with pytest.raises(DriverQuarantined) as captured:
        manager.connection()
    assert captured.value.reason_code == "health.revision_conflict"


def test_factory_extension_cannot_replace_an_active_driver() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )
    manager.extend("memory", lambda ref, options: _Driver(ref.name, _healthy()))
    with pytest.raises(InvalidManagerConfiguration, match="already registered"):
        manager.extend("memory", lambda ref, options: _Driver("replacement", _healthy()))

    manager.connection()
    with pytest.raises(InvalidManagerConfiguration, match="active"):
        manager.extend(
            "memory",
            lambda ref, options: _Driver("replacement", _healthy()),
            replace=True,
        )


def test_concurrent_sync_resolution_constructs_once() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )
    lock = Lock()
    calls = 0

    def build(ref: ConnectionRef, options: Mapping[str, object]) -> _Driver:
        del options
        nonlocal calls
        with lock:
            calls += 1
        return _Driver(ref.name, _healthy())

    manager.extend("memory", build)
    with ThreadPoolExecutor(max_workers=8) as pool:
        drivers = tuple(pool.map(lambda _: manager.connection(), range(8)))

    assert calls == 1
    assert len({id(driver) for driver in drivers}) == 1


def test_manager_disposes_cached_and_quarantined_drivers_once() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )
    driver = _Driver("primary", _healthy("cache.read", revision=1))
    manager.extend("memory", lambda ref, options: driver)
    manager.connection(required=CapabilitySet.of("cache.read"))
    driver.status = _healthy(revision=2)
    with pytest.raises(DriverQuarantined):
        manager.connection()

    manager.close()
    manager.close()
    assert driver.closed == 1


def test_factory_registration_validation_and_sync_awaitable_rejection() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )

    async def async_factory(
        ref: ConnectionRef,
        options: Mapping[str, object],
    ) -> _Driver:
        del options
        return _Driver(ref.name, _healthy())

    with pytest.raises(InvalidManagerConfiguration):
        manager.extend("memory", async_factory)
    with pytest.raises(InvalidManagerConfiguration):
        manager.extend("Bad Driver", lambda ref, options: _Driver(ref.name, _healthy()))
    with pytest.raises(InvalidManagerConfiguration):
        manager.extend("memory", object())  # type: ignore[arg-type]

    def returns_awaitable(
        ref: ConnectionRef,
        options: Mapping[str, object],
    ) -> object:
        del ref, options
        return asyncio.sleep(0)

    manager.extend("memory", returns_awaitable)  # type: ignore[arg-type]
    with pytest.raises(AsyncResolutionRequired):
        manager.connection()


def test_factory_failure_and_invalid_health_are_sanitized() -> None:
    failed: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("failure")},
        default="primary",
    )

    def fail(ref: ConnectionRef, options: Mapping[str, object]) -> _Driver:
        del ref, options
        raise RuntimeError("secret backend failure")

    failed.extend("failure", fail)
    with pytest.raises(Exception, match="driver construction failed") as captured:
        failed.connection()
    assert "secret backend failure" not in str(captured.value)

    class InvalidHealth:
        def health(self) -> object:
            return object()

    invalid: Manager[InvalidHealth] = Manager(
        "cache",
        {"primary": ConnectionConfig("invalid")},
        default="primary",
    )
    invalid.extend("invalid", lambda ref, options: InvalidHealth())
    with pytest.raises(Exception, match="invalid health evidence"):
        invalid.connection()

    class RaisingHealth:
        def health(self) -> HealthStatus:
            raise RuntimeError("private health failure")

    raising: Manager[RaisingHealth] = Manager(
        "cache",
        {"primary": ConnectionConfig("raising")},
        default="primary",
    )
    raising.extend("raising", lambda ref, options: RaisingHealth())
    with pytest.raises(Exception, match="health check failed") as health_error:
        raising.connection()
    assert "private health failure" not in str(health_error.value)


def test_initial_health_and_configured_capabilities_quarantine() -> None:
    unhealthy: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )
    unhealthy.extend(
        "memory",
        lambda ref, options: _Driver(
            ref.name,
            HealthStatus(
                HealthState.UNHEALTHY,
                reason_code="backend.down",
                revision=1,
            ),
        ),
    )
    with pytest.raises(DriverQuarantined) as unavailable:
        unhealthy.connection()
    assert unavailable.value.reason_code == "backend.down"

    missing: Manager[_Driver] = Manager(
        "cache",
        {
            "primary": ConnectionConfig(
                "memory",
                required_capabilities=CapabilitySet.of("cache.lock"),
            )
        },
        default="primary",
    )
    missing.extend("memory", lambda ref, options: _Driver(ref.name, _healthy("cache.read")))
    with pytest.raises(DriverQuarantined) as capability:
        missing.connection()
    assert capability.value.reason_code == "capability.configured_missing"


def test_health_revision_regression_and_public_health() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )
    driver = _Driver("primary", _healthy("cache.read", revision=2))
    manager.extend("memory", lambda ref, options: driver)
    with pytest.raises(Exception, match="has not been resolved"):
        manager.health()
    manager.connection(required=CapabilitySet.of("cache.read"))
    assert manager.health() == _healthy("cache.read", revision=2)
    driver.status = _healthy("cache.read", revision=1)
    with pytest.raises(DriverQuarantined) as captured:
        manager.health()
    assert captured.value.reason_code == "health.revision_regressed"


def test_invalidate_and_restore_are_explicit_and_do_not_fallback() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {
            "primary": ConnectionConfig("memory"),
            "secondary": ConnectionConfig("memory"),
        },
        default="primary",
    )
    built: list[_Driver] = []

    def factory(ref: ConnectionRef, options: Mapping[str, object]) -> _Driver:
        del options
        driver = _Driver(f"{ref.name}-{len(built)}", _healthy())
        built.append(driver)
        return driver

    manager.extend("memory", factory)
    first = manager.connection()
    manager.invalidate("primary")
    second = manager.connection()
    assert first is not second
    assert second.name.startswith("primary-")
    assert all(not driver.name.startswith("secondary-") for driver in built)
    assert manager.quarantine_reason("primary") is None
    with pytest.raises(UnknownConnection):
        manager.invalidate("missing")


def test_manager_close_requires_async_boundary_and_reports_failures() -> None:
    class AsyncOnlyDriver:
        def health(self) -> HealthStatus:
            return _healthy()

        async def aclose(self) -> None:
            await asyncio.sleep(0)

    async_only: Manager[AsyncOnlyDriver] = Manager(
        "cache",
        {"primary": ConnectionConfig("async-only")},
        default="primary",
    )
    driver = AsyncOnlyDriver()
    async_only.extend("async-only", lambda ref, options: driver)
    async_only.connection()
    with pytest.raises(Exception, match="asynchronous drivers"):
        async_only.close()
    asyncio.run(async_only.aclose())

    class FailingCloseDriver:
        def health(self) -> HealthStatus:
            return _healthy()

        def close(self) -> None:
            raise RuntimeError("close failed")

    failing: Manager[FailingCloseDriver] = Manager(
        "cache",
        {"primary": ConnectionConfig("failure")},
        default="primary",
    )
    failing.extend("failure", lambda ref, options: FailingCloseDriver())
    failing.connection()
    with pytest.raises(Exception, match="manager disposal failed") as captured:
        failing.close()
    assert len(captured.value.failures) == 1


def test_manager_rejects_use_after_close() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
        default="primary",
    )
    manager.extend("memory", lambda ref, options: _Driver(ref.name, _healthy()))
    manager.close()
    with pytest.raises(Exception, match="manager is closed"):
        manager.connection()
    with pytest.raises(Exception, match="manager is closed"):
        manager.extend("other", lambda ref, options: _Driver(ref.name, _healthy()))
