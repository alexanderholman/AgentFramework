"""Deployment-facing composition for supervised queue workers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from af.application.resource_leases import ResourceLeaseService, ResourceLeaseSweepMaintenance
from af.observability.signals import TelemetryPublisher
from af.queue.runtime import WorkerRuntime, WorkerSupervisor

__all__ = ("WorkerSupervisorFactory",)


class WorkerSupervisorFactory:
    """Build supervisors that cannot omit configured resource-lease maintenance."""

    def __init__(
        self,
        leases: ResourceLeaseService,
        *,
        host_id: str | None,
        maintenance_interval: timedelta,
    ) -> None:
        if not isinstance(leases, ResourceLeaseService):
            raise TypeError("worker supervisor factory requires a ResourceLeaseService")
        if host_id is not None and (not host_id or "\n" in host_id or "\r" in host_id):
            raise ValueError("worker supervisor maintenance host must be a single-line value")
        if maintenance_interval <= timedelta(0):
            raise ValueError("worker supervisor maintenance interval must be positive")
        self._leases = leases
        self._host_id = host_id
        self._maintenance_interval = maintenance_interval

    @property
    def host_id(self) -> str | None:
        return self._host_id

    @property
    def maintenance_interval(self) -> timedelta:
        return self._maintenance_interval

    def create(
        self,
        runtime_factory: Callable[[], WorkerRuntime],
        *,
        telemetry: TelemetryPublisher | None = None,
        max_restarts: int = 3,
        restart_delay: timedelta = timedelta(seconds=1),
        watch_interval: timedelta = timedelta(milliseconds=100),
        shutdown_timeout: timedelta = timedelta(seconds=30),
    ) -> WorkerSupervisor:
        """Create one supervisor with fresh maintenance evidence and caller-owned runtime input."""

        maintenance = ResourceLeaseSweepMaintenance(
            self._leases,
            host_id=self._host_id,
            telemetry=telemetry,
        )
        return WorkerSupervisor(
            runtime_factory,
            max_restarts=max_restarts,
            restart_delay=restart_delay,
            watch_interval=watch_interval,
            shutdown_timeout=shutdown_timeout,
            maintenance=maintenance,
            maintenance_interval=self._maintenance_interval,
        )
