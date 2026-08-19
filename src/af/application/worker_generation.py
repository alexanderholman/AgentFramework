"""Verified backend-specific composition for one owned worker generation."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from af.framework.managers import CapabilityBinding, CapabilityEvidence, ConnectionRef
from af.observability.signals import TelemetryPublisher
from af.queue.manager import QueueFeature, QueueManager
from af.queue.runtime import (
    OwnedManagerConnectionRecycler,
    PurgeableConnectionManager,
    WorkerRuntime,
)
from af.queue.worker import (
    IdempotencyStore,
    JobHandler,
    JobScopeFactory,
    WorkerAdmissionGate,
    WorkerKernel,
)

__all__ = (
    "PinnedWorkerOptimizationProvider",
    "WorkerGenerationFactory",
    "WorkerOptimizationContext",
    "WorkerOptimizationProvider",
    "WorkerRuntimeOptimization",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class WorkerRuntimeOptimization:
    """Explicit bounded runtime values produced by a backend optimizer."""

    worker_count: int
    max_concurrency: int
    poll_interval: timedelta
    heartbeat_interval: timedelta
    max_idle_time: timedelta | None
    queue_wait_alert_after: timedelta | None
    renewal_min_interval: timedelta
    renewal_max_interval: timedelta

    def __post_init__(self) -> None:
        if type(self.worker_count) is not int or self.worker_count < 1:
            raise ValueError("optimized worker count must be positive")
        if type(self.max_concurrency) is not int or self.max_concurrency < 1:
            raise ValueError("optimized maximum concurrency must be positive")
        if self.worker_count > self.max_concurrency:
            raise ValueError("optimized worker count exceeds maximum concurrency")
        if self.poll_interval <= timedelta(0):
            raise ValueError("optimized poll interval must be positive")
        if self.heartbeat_interval <= timedelta(0):
            raise ValueError("optimized heartbeat interval must be positive")
        if self.max_idle_time is not None and self.max_idle_time <= timedelta(0):
            raise ValueError("optimized maximum idle time must be positive")
        if self.queue_wait_alert_after is not None and self.queue_wait_alert_after <= timedelta(0):
            raise ValueError("optimized queue wait threshold must be positive")
        if self.renewal_min_interval <= timedelta(0):
            raise ValueError("optimized renewal minimum interval must be positive")
        if self.renewal_max_interval < self.renewal_min_interval:
            raise ValueError("optimized renewal maximum must not precede its minimum")


@dataclass(frozen=True, slots=True)
class WorkerOptimizationContext:
    """Verified connection identity and evidence available to an optimizer."""

    reference: ConnectionRef
    evidence: CapabilityEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ConnectionRef) or self.reference.manager_id != "queue":
            raise ValueError("worker optimization requires a queue connection reference")
        if not isinstance(self.evidence, CapabilityEvidence):
            raise ValueError("worker optimization requires capability evidence")


@runtime_checkable
class WorkerOptimizationProvider(Protocol):
    """Synchronous backend-specific optimizer boundary."""

    def __call__(self, context: WorkerOptimizationContext, /) -> WorkerRuntimeOptimization: ...


class PinnedWorkerOptimizationProvider:
    """Serve profiled values only for the exact capability binding they qualified."""

    def __init__(
        self,
        *,
        driver_name: str,
        binding: CapabilityBinding,
        profile_digest: str,
        qualification_digest: str,
        optimization: WorkerRuntimeOptimization,
        expires_at: datetime | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if type(driver_name) is not str or _IDENTIFIER.fullmatch(driver_name) is None:
            raise ValueError("pinned worker optimization driver must be canonical")
        if not isinstance(binding, CapabilityBinding):
            raise TypeError("pinned worker optimization requires a capability binding")
        if type(profile_digest) is not str or _DIGEST.fullmatch(profile_digest) is None:
            raise ValueError("worker optimization profile digest must be lowercase SHA-256")
        if type(qualification_digest) is not str or _DIGEST.fullmatch(qualification_digest) is None:
            raise ValueError("worker qualification digest must be lowercase SHA-256")
        if not isinstance(optimization, WorkerRuntimeOptimization):
            raise TypeError("pinned worker optimization requires runtime values")
        if expires_at is not None and (
            not isinstance(expires_at, datetime) or expires_at.tzinfo is None
        ):
            raise ValueError("worker optimization expiry must be timezone-aware")
        if not callable(clock):
            raise TypeError("worker optimization clock must be callable")
        self._driver_name = driver_name
        self._binding = binding
        self._profile_digest = profile_digest
        self._qualification_digest = qualification_digest
        self._optimization = optimization
        self._expires_at = expires_at
        self._clock = clock

    @property
    def profile_digest(self) -> str:
        return self._profile_digest

    @property
    def qualification_digest(self) -> str:
        return self._qualification_digest

    @property
    def driver_name(self) -> str:
        return self._driver_name

    @property
    def binding(self) -> CapabilityBinding:
        return self._binding

    def __call__(self, context: WorkerOptimizationContext, /) -> WorkerRuntimeOptimization:
        if not isinstance(context, WorkerOptimizationContext):
            raise TypeError("worker optimization context is invalid")
        if context.reference.driver_name != self._driver_name:
            raise ValueError("worker optimization backend identity changed")
        if context.evidence.binding != self._binding:
            raise ValueError("worker optimization capability binding changed")
        if self._expires_at is not None and self._clock() >= self._expires_at:
            raise ValueError("worker optimization qualification is expired")
        return self._optimization


def _is_async_callable(value: object) -> bool:
    if inspect.iscoroutinefunction(value) or inspect.isasyncgenfunction(value):
        return True
    call = inspect.getattr_static(value, "__call__", None)
    return inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call)


def _sync_result(value: object, field: str) -> object:
    if inspect.iscoroutine(value):
        value.close()
        raise TypeError(f"{field} returned an asynchronous result")
    if inspect.isawaitable(value) or inspect.isasyncgen(value):
        raise TypeError(f"{field} returned an asynchronous result")
    return value


class WorkerGenerationFactory:
    """Build fresh workers from verified managers and backend-specific profiles.

    The manager factory transfers one fresh queue manager to each generation. The kernel closes
    that manager, not its borrowed driver, preserving the manager's disposal ownership. Any
    managers passed as ``owned_connection_managers`` are explicitly worker-owned handler-side
    managers and are purged after every settled delivery; queue managers are rejected there.
    """

    def __init__(
        self,
        manager_factory: Callable[[], QueueManager],
        *,
        optimizers: Mapping[str, WorkerOptimizationProvider],
    ) -> None:
        if not callable(manager_factory) or _is_async_callable(manager_factory):
            raise TypeError("worker generation manager factory must be synchronous")
        if not isinstance(optimizers, Mapping):
            raise TypeError("worker generation optimizers must be a mapping")
        copied: dict[str, WorkerOptimizationProvider] = {}
        for driver_name, optimizer in optimizers.items():
            if type(driver_name) is not str or _IDENTIFIER.fullmatch(driver_name) is None:
                raise ValueError("worker optimizer driver must be a canonical identifier")
            if not callable(optimizer) or _is_async_callable(optimizer):
                raise TypeError("worker optimization provider must be synchronous")
            copied[driver_name] = optimizer
        self._manager_factory = manager_factory
        self._optimizers = copied

    def create(
        self,
        handler: JobHandler,
        *,
        connection: str | None = None,
        lane: str | None = None,
        owned_connection_managers: tuple[PurgeableConnectionManager, ...] = (),
        idempotency: IdempotencyStore | None = None,
        scope_factory: JobScopeFactory | None = None,
        admission_gate: WorkerAdmissionGate | None = None,
        telemetry: TelemetryPublisher | None = None,
    ) -> WorkerRuntime:
        manager_result = _sync_result(self._manager_factory(), "worker manager factory")
        if not isinstance(manager_result, QueueManager):
            raise TypeError("worker manager factory must return a QueueManager")
        manager = manager_result
        if manager.resolved_connections:
            raise ValueError("worker manager factory must return a fresh QueueManager")
        try:
            reference = manager.reference(connection)
            driver = manager.connection(connection, requires=(QueueFeature.RESERVATION,))
            evidence = manager.evidence(connection)
            optimizer = self._optimizers.get(reference.driver_name)
            if optimizer is None:
                raise LookupError("worker backend optimization provider is not configured")
            context = WorkerOptimizationContext(reference=reference, evidence=evidence)
            optimized_result = _sync_result(optimizer(context), "worker optimization provider")
            if not isinstance(optimized_result, WorkerRuntimeOptimization):
                raise TypeError("worker optimization provider returned invalid runtime values")
            optimization = optimized_result
            if any(isinstance(owned, QueueManager) for owned in owned_connection_managers):
                raise ValueError("worker-owned connection recycling cannot include a queue manager")
            recycler = (
                None
                if not owned_connection_managers
                else OwnedManagerConnectionRecycler(*owned_connection_managers)
            )
            kernel = WorkerKernel(
                driver,
                max_concurrency=optimization.max_concurrency,
                heartbeat_interval=optimization.heartbeat_interval,
                max_idle_time=optimization.max_idle_time,
                idempotency=idempotency,
                scope_factory=scope_factory,
                admission_gate=admission_gate,
                connection_recycler=recycler,
                driver_close=manager.close,
                telemetry=telemetry,
                queue_wait_alert_after=optimization.queue_wait_alert_after,
                renewal_min_interval=optimization.renewal_min_interval,
                renewal_max_interval=optimization.renewal_max_interval,
            )
            return WorkerRuntime(
                kernel,
                handler,
                worker_count=optimization.worker_count,
                lane=lane,
                poll_interval=optimization.poll_interval,
            )
        except BaseException as error:
            try:
                manager.close()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(
                    "worker generation composition and rollback both failed",
                    (error, cleanup_error),
                ) from None
            raise
