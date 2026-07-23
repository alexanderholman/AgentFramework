"""Value contracts for deterministic named-driver managers."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeVar

from af.framework.errors import AFError

__all__ = (
    "AsyncDriverFactory",
    "CapabilitySet",
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
    "ManagerDisposalError",
    "ManagerError",
    "UnknownConnection",
    "UnknownDriver",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_REASON = re.compile(r"^[a-z0-9](?:[a-z0-9_.:-]{0,126}[a-z0-9])?$")
_MAX_CONNECTIONS = 1024
_MAX_CAPABILITIES = 256
_MAX_OPTIONS = 256


class ManagerError(AFError):
    """Base class for deterministic manager failures."""


class InvalidManagerConfiguration(ManagerError, ValueError):
    """Raised before a malformed manager declaration can resolve a driver."""


class UnknownConnection(ManagerError):
    """Raised when a configured connection cannot be selected."""


class UnknownDriver(ManagerError):
    """Raised when no registered factory owns the configured driver identity."""


class DriverUnavailable(ManagerError):
    """Raised when a driver is not currently eligible to serve work."""

    def __init__(
        self,
        message: str = "driver is unavailable",
        *,
        protected_cause: BaseException | None = None,
    ) -> None:
        self.protected_cause = protected_cause
        super().__init__(message)


class DriverCapabilityUnavailable(DriverUnavailable):
    """Raised when the selected driver lacks an explicitly required capability."""

    def __init__(self, missing: CapabilitySet) -> None:
        self.missing = missing
        super().__init__("required driver capability is unavailable")


class DriverQuarantined(DriverUnavailable):
    """Raised after health or capability evidence invalidates a cached connection."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__("driver connection is quarantined")


class DriverConstructionFailed(DriverUnavailable):
    """Sanitized factory failure retaining its cause for trusted diagnostics."""

    def __init__(self, *, protected_cause: BaseException) -> None:
        super().__init__("driver construction failed", protected_cause=protected_cause)


class ManagerDisposalError(ManagerError):
    """Raised after best-effort disposal of all manager-owned driver instances."""

    def __init__(self, failures: tuple[BaseException, ...]) -> None:
        self.failures = failures
        super().__init__(f"manager disposal failed for {len(failures)} driver(s)")


def _canonical_identifier(value: object, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise InvalidManagerConfiguration(f"{label} must be a canonical identifier")
    return value


def _canonical_reason(value: object, label: str) -> str:
    if type(value) is not str or _REASON.fullmatch(value) is None:
        raise InvalidManagerConfiguration(f"{label} must be a canonical reason code")
    return value


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    """Finite immutable set of provider-neutral capability identities."""

    values: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.values) is not frozenset or len(self.values) > _MAX_CAPABILITIES:
            raise InvalidManagerConfiguration("capability set is invalid")
        normalized = frozenset(
            _canonical_identifier(value, "capability") for value in self.values
        )
        object.__setattr__(self, "values", normalized)

    @classmethod
    def of(cls, *values: str) -> CapabilitySet:
        return cls(frozenset(values))

    def union(self, *others: CapabilitySet) -> CapabilitySet:
        values = set(self.values)
        for other in others:
            if not isinstance(other, CapabilitySet):
                raise TypeError("capability union requires CapabilitySet values")
            values.update(other.values)
        return CapabilitySet(frozenset(values))

    def missing_from(self, available: CapabilitySet) -> CapabilitySet:
        if not isinstance(available, CapabilitySet):
            raise TypeError("available capabilities must be a CapabilitySet")
        return CapabilitySet(self.values - available.values)

    def is_subset_of(self, available: CapabilitySet) -> bool:
        return not self.missing_from(available).values

    def __bool__(self) -> bool:
        return bool(self.values)

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self.values))


class HealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """One monotonic health/capability observation for a driver instance."""

    state: HealthState
    capabilities: CapabilitySet = field(default_factory=CapabilitySet)
    reason_code: str | None = None
    revision: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.state, HealthState):
            raise InvalidManagerConfiguration("health state is invalid")
        if not isinstance(self.capabilities, CapabilitySet):
            raise InvalidManagerConfiguration("health capabilities are invalid")
        if self.reason_code is not None:
            _canonical_reason(self.reason_code, "health reason")
        if type(self.revision) is not int or self.revision < 0:
            raise InvalidManagerConfiguration("health revision must be a non-negative integer")

    @property
    def serving(self) -> bool:
        return self.state in {HealthState.HEALTHY, HealthState.DEGRADED}


class ManagedDriver(Protocol):
    """Minimum live evidence contract required by every manager-owned driver."""

    def health(self) -> HealthStatus: ...


DriverT = TypeVar("DriverT", bound=ManagedDriver)
DriverT_co = TypeVar("DriverT_co", bound=ManagedDriver, covariant=True)


class DriverFactory(Protocol[DriverT_co]):
    def __call__(
        self,
        connection: ConnectionRef,
        options: Mapping[str, object],
    ) -> DriverT_co: ...


class AsyncDriverFactory(Protocol[DriverT_co]):
    def __call__(
        self,
        connection: ConnectionRef,
        options: Mapping[str, object],
    ) -> Awaitable[DriverT_co]: ...


class CompositeMode(StrEnum):
    FALLBACK = "fallback"
    FAILOVER = "failover"
    ROUND_ROBIN = "round_robin"


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    name: str
    required_capabilities: CapabilitySet = field(default_factory=CapabilitySet)

    def __post_init__(self) -> None:
        _canonical_identifier(self.name, "connection name")
        if not isinstance(self.required_capabilities, CapabilitySet):
            raise InvalidManagerConfiguration("connection requirements are invalid")


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    driver: str
    options: Mapping[str, object] = field(default_factory=dict, repr=False)
    required_capabilities: CapabilitySet = field(default_factory=CapabilitySet)

    def __post_init__(self) -> None:
        _canonical_identifier(self.driver, "driver id")
        if not isinstance(self.options, Mapping) or len(self.options) > _MAX_OPTIONS:
            raise InvalidManagerConfiguration("driver options must be a bounded mapping")
        copied: dict[str, object] = {}
        for key, value in self.options.items():
            if (
                type(key) is not str
                or not key
                or len(key) > 200
                or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", key)
            ):
                raise InvalidManagerConfiguration("driver option keys are invalid")
            copied[key] = value
        if not isinstance(self.required_capabilities, CapabilitySet):
            raise InvalidManagerConfiguration("connection capabilities are invalid")
        object.__setattr__(self, "options", MappingProxyType(copied))
