"""Deterministic named-driver managers and explicit composite routes."""

from af.framework._manager_composite import CompositeDriver
from af.framework._manager_contracts import (
    AsyncDriverFactory,
    CapabilitySet,
    CompositeMode,
    ConnectionConfig,
    ConnectionRef,
    DriverCapabilityUnavailable,
    DriverConstructionFailed,
    DriverFactory,
    DriverQuarantined,
    DriverUnavailable,
    HealthState,
    HealthStatus,
    InvalidManagerConfiguration,
    ManagedDriver,
    ManagerDisposalError,
    ManagerError,
    UnknownConnection,
    UnknownDriver,
)
from af.framework._manager_runtime import Manager

__all__ = (
    "AsyncDriverFactory",
    "CapabilitySet",
    "CompositeDriver",
    "CompositeMode",
    "ConnectionConfig",
    "ConnectionRef",
    "DriverCapabilityUnavailable",
    "DriverConstructionFailed",
    "DriverFactory",
    "DriverQuarantined",
    "DriverUnavailable",
    "HealthState",
    "HealthStatus",
    "InvalidManagerConfiguration",
    "ManagedDriver",
    "Manager",
    "ManagerDisposalError",
    "ManagerError",
    "UnknownConnection",
    "UnknownDriver",
)
