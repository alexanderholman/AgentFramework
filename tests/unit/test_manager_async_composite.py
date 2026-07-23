from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from af.framework.errors import AsyncResolutionRequired
from af.framework.managers import (
    CapabilitySet,
    CompositeMode,
    ConnectionConfig,
    ConnectionRef,
    HealthState,
    HealthStatus,
    InvalidManagerConfiguration,
    Manager,
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


def test_async_factory_is_shared_by_concurrent_waiters() -> None:
    async def scenario() -> None:
        manager: Manager[_Driver] = Manager(
            "cache",
            {"primary": ConnectionConfig("async")},
            default="primary",
        )
        calls = 0

        async def build(ref: ConnectionRef, options: Mapping[str, object]) -> _Driver:
            del options
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return _Driver(ref.name, _healthy("cache.read"))

        manager.extend_async("async", build)
        with pytest.raises(AsyncResolutionRequired):
            manager.connection()
        drivers = await asyncio.gather(*(manager.aconnection() for _ in range(8)))
        assert calls == 1
        assert len({id(driver) for driver in drivers}) == 1
        await manager.aclose()

    asyncio.run(scenario())


def test_failover_and_round_robin_are_explicit_composites() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {
            "first": ConnectionConfig("memory"),
            "second": ConnectionConfig("memory"),
        },
    )
    drivers = {
        "first": _Driver(
            "first",
            HealthStatus(HealthState.UNHEALTHY, reason_code="backend.down", revision=1),
        ),
        "second": _Driver("second", _healthy("cache.read", revision=1)),
    }
    manager.extend("memory", lambda ref, options: drivers[ref.name])

    failover = manager.composite(
        CompositeMode.FAILOVER,
        (ConnectionRef("first"), ConnectionRef("second")),
        required=CapabilitySet.of("cache.read"),
    )
    assert failover.select() is drivers["second"]

    manager.restore("first")
    drivers["first"] = _Driver("first-new", _healthy("cache.read", revision=2))
    manager.invalidate("second")
    drivers["second"] = _Driver("second-new", _healthy("cache.read", revision=2))
    round_robin = manager.composite(
        CompositeMode.ROUND_ROBIN,
        (ConnectionRef("first"), ConnectionRef("second")),
        required=CapabilitySet.of("cache.read"),
    )
    assert [round_robin.select().name for _ in range(4)] == [
        "first-new",
        "second-new",
        "first-new",
        "second-new",
    ]


def test_aconnection_supports_sync_factory_and_async_factory_failures() -> None:
    async def scenario() -> None:
        sync_manager: Manager[_Driver] = Manager(
            "cache",
            {"primary": ConnectionConfig("memory")},
            default="primary",
        )
        sync_manager.extend(
            "memory",
            lambda ref, options: _Driver(ref.name, _healthy("cache.read")),
        )
        assert (await sync_manager.aconnection()).name == "primary"
        await sync_manager.aclose()

        invalid: Manager[_Driver] = Manager(
            "cache",
            {"primary": ConnectionConfig("async")},
            default="primary",
        )

        def not_awaitable(
            ref: ConnectionRef,
            options: Mapping[str, object],
        ) -> object:
            del ref, options
            return object()

        invalid.extend_async("async", not_awaitable)  # type: ignore[arg-type]
        with pytest.raises(InvalidManagerConfiguration, match="returned no awaitable"):
            await invalid.aconnection()

        failed: Manager[_Driver] = Manager(
            "cache",
            {"primary": ConnectionConfig("async")},
            default="primary",
        )

        async def fail(
            ref: ConnectionRef,
            options: Mapping[str, object],
        ) -> _Driver:
            del ref, options
            raise RuntimeError("secret async failure")

        failed.extend_async("async", fail)
        with pytest.raises(Exception, match="driver construction failed"):
            await failed.aconnection()

    asyncio.run(scenario())


def test_async_factory_cancellation_clears_construction_flight() -> None:
    async def scenario() -> None:
        manager: Manager[_Driver] = Manager(
            "cache",
            {"primary": ConnectionConfig("async")},
            default="primary",
        )
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def build(
            ref: ConnectionRef,
            options: Mapping[str, object],
        ) -> _Driver:
            del options
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return _Driver(ref.name, _healthy())

        manager.extend_async("async", build)
        task = asyncio.create_task(manager.aconnection())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert (await manager.aconnection()).name == "primary"
        assert calls == 2
        await manager.aclose()

    asyncio.run(scenario())


def test_composite_validation_selection_and_health_states() -> None:
    manager: Manager[_Driver] = Manager(
        "cache",
        {
            "first": ConnectionConfig("memory"),
            "second": ConnectionConfig("memory"),
        },
    )
    drivers = {
        "first": _Driver("first", _healthy("cache.read", revision=1)),
        "second": _Driver("second", _healthy("cache.read", revision=2)),
    }
    manager.extend("memory", lambda ref, options: drivers[ref.name])

    with pytest.raises(InvalidManagerConfiguration):
        manager.composite(CompositeMode.FAILOVER, (ConnectionRef("first"),))
    with pytest.raises(InvalidManagerConfiguration):
        manager.composite(
            CompositeMode.FAILOVER,
            (ConnectionRef("first"), ConnectionRef("first")),
        )
    with pytest.raises(InvalidManagerConfiguration):
        manager.composite(
            "failover",  # type: ignore[arg-type]
            (ConnectionRef("first"), ConnectionRef("second")),
        )

    fallback = manager.composite(
        CompositeMode.FALLBACK,
        (ConnectionRef("first"), ConnectionRef("second")),
    )
    assert fallback.mode is CompositeMode.FALLBACK
    assert fallback.select() is drivers["first"]

    composite = manager.composite(
        CompositeMode.FAILOVER,
        (ConnectionRef("first"), ConnectionRef("second")),
        required=CapabilitySet.of("cache.read"),
    )
    assert composite.mode is CompositeMode.FAILOVER
    assert tuple(ref.name for ref in composite.connections) == ("first", "second")
    assert composite.select() is drivers["first"]
    health = composite.health()
    assert health.state is HealthState.HEALTHY
    assert health.capabilities == CapabilitySet.of("cache.read")
    assert health.revision == 1

    drivers["first"].status = HealthStatus(
        HealthState.UNHEALTHY,
        reason_code="backend.down",
        revision=2,
    )
    degraded = composite.health()
    assert degraded.state is HealthState.DEGRADED
    assert degraded.reason_code == "composite.degraded"


def test_composite_fails_when_no_member_is_eligible_and_supports_async_selection() -> None:
    async def scenario() -> None:
        manager: Manager[_Driver] = Manager(
            "cache",
            {
                "first": ConnectionConfig("memory"),
                "second": ConnectionConfig("memory"),
            },
        )
        drivers = {
            name: _Driver(
                name,
                HealthStatus(
                    HealthState.UNHEALTHY,
                    reason_code="backend.down",
                    revision=1,
                ),
            )
            for name in ("first", "second")
        }
        manager.extend("memory", lambda ref, options: drivers[ref.name])
        composite = manager.composite(
            CompositeMode.FAILOVER,
            (ConnectionRef("first"), ConnectionRef("second")),
        )
        with pytest.raises(Exception, match="no eligible driver"):
            composite.select()
        assert composite.health().reason_code == "composite.no_eligible_member"

        manager.restore("first")
        drivers["first"] = _Driver("first-healthy", _healthy())
        assert (await composite.aselect()).name == "first-healthy"
        await manager.aclose()

    asyncio.run(scenario())
