from __future__ import annotations

from dataclasses import dataclass

import pytest

from af.framework.managers import (
    CapabilitySet,
    ConnectionConfig,
    ConnectionRef,
    HealthState,
    HealthStatus,
    InvalidManagerConfiguration,
    Manager,
    UnknownConnection,
    UnknownDriver,
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


def test_capability_and_health_value_validation() -> None:
    assert tuple(CapabilitySet.of("cache.write", "cache.read")) == (
        "cache.read",
        "cache.write",
    )
    assert bool(CapabilitySet.of("cache.read")) is True
    assert bool(CapabilitySet()) is False
    assert CapabilitySet.of("cache.read").union(CapabilitySet.of("cache.write")) == (
        CapabilitySet.of("cache.read", "cache.write")
    )
    assert CapabilitySet.of("cache.read").is_subset_of(
        CapabilitySet.of("cache.read", "cache.write")
    )
    with pytest.raises(TypeError):
        CapabilitySet.of("cache.read").union("cache.write")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        CapabilitySet.of("cache.read").missing_from("cache.read")  # type: ignore[arg-type]
    with pytest.raises(InvalidManagerConfiguration):
        CapabilitySet(frozenset({"Bad Capability"}))
    with pytest.raises(InvalidManagerConfiguration):
        CapabilitySet(set())  # type: ignore[arg-type]

    assert HealthStatus(HealthState.DEGRADED).serving is True
    assert HealthStatus(HealthState.UNKNOWN).serving is False
    with pytest.raises(InvalidManagerConfiguration):
        HealthStatus("healthy")  # type: ignore[arg-type]
    with pytest.raises(InvalidManagerConfiguration):
        HealthStatus(HealthState.HEALTHY, "cache.read")  # type: ignore[arg-type]
    with pytest.raises(InvalidManagerConfiguration):
        HealthStatus(HealthState.UNHEALTHY, reason_code="not valid!")
    with pytest.raises(InvalidManagerConfiguration):
        HealthStatus(HealthState.HEALTHY, revision=-1)


def test_connection_value_validation_and_option_snapshot() -> None:
    options = {"host": "localhost"}
    config = ConnectionConfig(
        "memory",
        options,
        CapabilitySet.of("cache.read"),
    )
    options["host"] = "changed"
    assert config.options == {"host": "localhost"}
    with pytest.raises(TypeError):
        config.options["host"] = "forbidden"  # type: ignore[index]
    assert ConnectionRef("primary", CapabilitySet.of("cache.read")).name == "primary"

    with pytest.raises(InvalidManagerConfiguration):
        ConnectionRef("Bad Name")
    with pytest.raises(InvalidManagerConfiguration):
        ConnectionRef("primary", "cache.read")  # type: ignore[arg-type]
    with pytest.raises(InvalidManagerConfiguration):
        ConnectionConfig("Bad Driver")
    with pytest.raises(InvalidManagerConfiguration):
        ConnectionConfig("memory", [])  # type: ignore[arg-type]
    with pytest.raises(InvalidManagerConfiguration):
        ConnectionConfig("memory", {"bad\nkey": True})
    with pytest.raises(InvalidManagerConfiguration):
        ConnectionConfig("memory", required_capabilities="cache.read")


def test_manager_configuration_is_bounded_and_immutable() -> None:
    source = {"primary": ConnectionConfig("memory")}
    manager: Manager[_Driver] = Manager("cache", source, default="primary")
    source["secondary"] = ConnectionConfig("memory")
    assert manager.manager_id == "cache"
    assert manager.default_connection == "primary"
    assert manager.configured_connections == ("primary",)

    with pytest.raises(InvalidManagerConfiguration):
        Manager("Bad Manager", {})
    with pytest.raises(InvalidManagerConfiguration):
        Manager("cache", [])  # type: ignore[arg-type]
    with pytest.raises(InvalidManagerConfiguration):
        Manager("cache", {"Bad Name": ConnectionConfig("memory")})
    with pytest.raises(InvalidManagerConfiguration):
        Manager("cache", {"primary": object()})  # type: ignore[dict-item]
    with pytest.raises(InvalidManagerConfiguration):
        Manager("cache", {"primary": ConnectionConfig("memory")}, default="missing")


def test_unknown_connection_driver_and_missing_default_fail_closed() -> None:
    no_default: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("memory")},
    )
    with pytest.raises(UnknownConnection):
        no_default.connection()
    with pytest.raises(UnknownConnection):
        no_default.connection("missing")

    unknown: Manager[_Driver] = Manager(
        "cache",
        {"primary": ConnectionConfig("unregistered")},
        default="primary",
    )
    with pytest.raises(UnknownDriver):
        unknown.connection()
