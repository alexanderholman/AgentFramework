"""Explicit fallback, failover, and round-robin driver composition."""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Generic, TypeVar

from af.framework._manager_contracts import (
    CapabilitySet,
    CompositeMode,
    ConnectionRef,
    DriverUnavailable,
    HealthState,
    HealthStatus,
    InvalidManagerConfiguration,
    ManagedDriver,
    _MAX_CONNECTIONS,
)

if TYPE_CHECKING:
    from af.framework._manager_runtime import Manager

__all__ = ("CompositeDriver",)

DriverT = TypeVar("DriverT", bound=ManagedDriver)


class CompositeDriver(Generic[DriverT]):
    """Explicit fallback, failover, or round-robin route."""

    def __init__(
        self,
        manager: Manager[DriverT],
        mode: CompositeMode,
        connections: tuple[ConnectionRef, ...],
        *,
        required: CapabilitySet,
    ) -> None:
        if not callable(getattr(manager, "connection", None)):
            raise TypeError("composite manager is invalid")
        if not isinstance(mode, CompositeMode):
            raise InvalidManagerConfiguration("composite mode is invalid")
        if len(connections) < 2 or len(connections) > _MAX_CONNECTIONS:
            raise InvalidManagerConfiguration("composite requires at least two bounded members")
        if any(not isinstance(item, ConnectionRef) for item in connections):
            raise InvalidManagerConfiguration("composite connection is invalid")
        if len({item.name for item in connections}) != len(connections):
            raise InvalidManagerConfiguration("composite connections must be unique")
        if not isinstance(required, CapabilitySet):
            raise InvalidManagerConfiguration("composite capabilities are invalid")
        self._manager = manager
        self._mode = mode
        self._connections = connections
        self._required = required
        self._cursor = 0
        self._lock = RLock()

    @property
    def mode(self) -> CompositeMode:
        return self._mode

    @property
    def connections(self) -> tuple[ConnectionRef, ...]:
        return self._connections

    def select(self) -> DriverT:
        failures: list[DriverUnavailable] = []
        with self._lock:
            indices = self._candidate_indices()
            for index in indices:
                ref = self._connections[index]
                required = self._required.union(ref.required_capabilities)
                try:
                    driver = self._manager.connection(ref.name, required=required)
                except DriverUnavailable as error:
                    failures.append(error)
                    continue
                if self._mode is CompositeMode.ROUND_ROBIN:
                    self._cursor = (index + 1) % len(self._connections)
                return driver
        raise DriverUnavailable(
            "explicit composite has no eligible driver",
            protected_cause=failures[0] if failures else None,
        )

    async def aselect(self) -> DriverT:
        failures: list[DriverUnavailable] = []
        with self._lock:
            indices = self._candidate_indices()
        for index in indices:
            ref = self._connections[index]
            required = self._required.union(ref.required_capabilities)
            try:
                driver = await self._manager.aconnection(ref.name, required=required)
            except DriverUnavailable as error:
                failures.append(error)
                continue
            if self._mode is CompositeMode.ROUND_ROBIN:
                with self._lock:
                    self._cursor = (index + 1) % len(self._connections)
            return driver
        raise DriverUnavailable(
            "explicit composite has no eligible driver",
            protected_cause=failures[0] if failures else None,
        )

    def health(self) -> HealthStatus:
        statuses: list[HealthStatus] = []
        for ref in self._connections:
            try:
                self._manager.connection(
                    ref.name,
                    required=self._required.union(ref.required_capabilities),
                )
                statuses.append(self._manager.health(ref.name))
            except DriverUnavailable:
                continue
        if not statuses:
            return HealthStatus(
                HealthState.UNHEALTHY,
                reason_code="composite.no_eligible_member",
            )
        common = set(statuses[0].capabilities.values)
        for status in statuses[1:]:
            common.intersection_update(status.capabilities.values)
        all_members = len(statuses) == len(self._connections)
        all_healthy = all(status.state is HealthState.HEALTHY for status in statuses)
        return HealthStatus(
            HealthState.HEALTHY if all_members and all_healthy else HealthState.DEGRADED,
            CapabilitySet(frozenset(common)),
            None if all_members and all_healthy else "composite.degraded",
            revision=min(status.revision for status in statuses),
        )

    def _candidate_indices(self) -> tuple[int, ...]:
        if self._mode in {CompositeMode.FALLBACK, CompositeMode.FAILOVER}:
            return tuple(range(len(self._connections)))
        return tuple(
            (self._cursor + offset) % len(self._connections)
            for offset in range(len(self._connections))
        )
