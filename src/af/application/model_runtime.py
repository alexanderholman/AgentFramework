"""Exclusive local residency for exact, already authorized runtime variants.

The broker never chooses a model, fetches content, or falls back to a provider.
Adapters must honor load cancellation/deadlines and terminate their own children.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from threading import Condition, Event, RLock
from time import monotonic
from typing import Protocol
from uuid import uuid4

from af.application.resource_leases import ResourceLeaseService
from af.domain.resources import Lease, LeaseState, ResourceBudget
from af.framework.errors import AFError
from af.observability.signals import TelemetryKind, TelemetryPublisher, TelemetrySignal


class RuntimeRefused(AFError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class RuntimeState(StrEnum):
    COLD = "cold"
    QUEUED = "queued"
    LOADING = "loading"
    WARM = "warm"
    BUSY = "busy"
    DRAINING = "draining"
    EVICTING = "evicting"
    FAILED = "failed"
    CANCELLED = "cancelled"
    QUARANTINED = "quarantined"


def _digest(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"sha256:[a-f0-9]{64}", value) is None:
        raise ValueError("runtime identities require canonical SHA-256 digests")


def _aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("runtime timestamps must be timezone aware")


@dataclass(frozen=True, slots=True)
class RuntimeVariant:
    variant_id: str
    artifact_digest: str
    runtime_digest: str
    adapter_digest: str
    compatibility_digest: str
    memory_mib: int

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", self.variant_id) is None:
            raise ValueError("runtime variant identity must be canonical")
        for value in (
            self.artifact_digest,
            self.runtime_digest,
            self.adapter_digest,
            self.compatibility_digest,
        ):
            _digest(value)
        if type(self.memory_mib) is not int or not 1 <= self.memory_mib <= 1_000_000_000:
            raise ValueError("runtime memory reservation must be positive and bounded")

    @property
    def digest(self) -> str:
        data = json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeInvocation:
    invocation_id: str
    variant: RuntimeVariant
    policy_digest: str
    scope_digest: str
    authorized_by: str
    authorization_digest: str
    deadline: datetime
    authorized_until: datetime
    cancellation: Event

    def __post_init__(self) -> None:
        for value in (self.invocation_id, self.authorized_by):
            if not value or len(value) > 128 or any(ord(char) < 32 for char in value):
                raise ValueError("runtime invocation identities must be bounded")
        for digest in (self.policy_digest, self.scope_digest, self.authorization_digest):
            _digest(digest)
        _aware(self.deadline)
        _aware(self.authorized_until)
        if not isinstance(self.variant, RuntimeVariant) or not isinstance(self.cancellation, Event):
            raise ValueError("runtime invocation requires a typed variant and cancellation")


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    process_alive: bool
    endpoint_responsive: bool
    runtime_ready: bool
    model_ready: bool
    variant_digest: str
    reason_code: str = "observed"

    def ready_for(self, variant: RuntimeVariant) -> bool:
        return (
            self.process_alive
            and self.endpoint_responsive
            and self.runtime_ready
            and self.model_ready
            and self.variant_digest == variant.digest
        )


class RuntimeAdapter(Protocol):
    def load(self, variant: RuntimeVariant, cancellation: Event, deadline: datetime) -> None: ...

    def health(self, variant: RuntimeVariant) -> RuntimeHealth: ...

    def unload(self, variant: RuntimeVariant) -> None: ...


@dataclass(frozen=True, slots=True)
class RuntimeTransition:
    sequence: int
    state: RuntimeState
    reason_code: str
    observed_at: datetime
    variant_digest: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "state": self.state.value,
            "reason_code": self.reason_code,
            "observed_at": self.observed_at.isoformat(),
            "variant_digest": self.variant_digest,
        }


class ModelRuntimeBroker:
    def __init__(
        self,
        adapter: RuntimeAdapter,
        leases: ResourceLeaseService,
        budget: ResourceBudget,
        *,
        authorize: Callable[[RuntimeInvocation], bool],
        max_queue: int = 16,
        max_load_failures: int = 3,
        idle_seconds: float = 300,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        telemetry: TelemetryPublisher | None = None,
    ) -> None:
        if max_queue < 1 or max_load_failures < 1 or not 0 <= idle_seconds <= 86400:
            raise ValueError("runtime queue, restart and idle limits must be bounded")
        if budget.max_duration is None or budget.max_concurrent != 1:
            raise ValueError("runtime broker requires an expiring exclusive residency budget")
        self.adapter, self.leases, self.budget = adapter, leases, budget
        self.authorize = authorize
        self._clock, self._telemetry = clock, telemetry
        self._max_queue, self._max_load_failures = max_queue, max_load_failures
        self._idle_seconds = idle_seconds
        self._condition = Condition(RLock())
        self._pending: deque[RuntimeInvocation] = deque()
        self._active: RuntimeInvocation | None = None
        self._resident: RuntimeInvocation | None = None
        self._lease: Lease | None = None
        self._state = RuntimeState.COLD
        self._history: deque[RuntimeTransition] = deque(maxlen=1024)
        self._sequence = 0
        self._last_used = self._clock()
        self._failures = 0
        self._closed = False
        self._pinned = False

    @property
    def state(self) -> RuntimeState:
        with self._condition:
            return self._state

    @property
    def transitions(self) -> tuple[RuntimeTransition, ...]:
        with self._condition:
            return tuple(self._history)

    def execute[T](self, invocation: RuntimeInvocation, work: Callable[[], T]) -> T:
        self._enter(invocation)
        try:
            self._ensure_resident(invocation)
            self._validate(invocation)
            self._check_lease()
            self._transition(RuntimeState.BUSY, "dispatch", invocation.variant)
            result = work()
            self._validate(invocation)
            self._check_lease()
            if not self.adapter.health(invocation.variant).ready_for(invocation.variant):
                raise RuntimeRefused("runtime_health_failed")
            self._validate(invocation)
            self._check_lease()
            self._last_used = self._clock()
            self._transition(RuntimeState.WARM, "work_finished", invocation.variant)
            return result
        except BaseException as error:
            self._cleanup_after_failure(invocation, error)
            if isinstance(error, RuntimeRefused | KeyboardInterrupt | SystemExit):
                raise
            raise RuntimeRefused("runtime_failed") from error
        finally:
            with self._condition:
                self._active = None
                self._condition.notify_all()

    def _enter(self, invocation: RuntimeInvocation) -> None:
        with self._condition:
            if self._active is None and self._resident is not None:
                try:
                    self._validate(self._resident, residency=True)
                    self._check_lease()
                except RuntimeRefused:
                    self._evict("residency_revoked")
                    raise
            self.maintain()
            self._validate(invocation)
            if self._state is RuntimeState.QUARANTINED:
                raise RuntimeRefused("runtime_quarantined")
            if len(self._pending) >= self._max_queue:
                raise RuntimeRefused("queue_full")
            if any(item.invocation_id == invocation.invocation_id for item in self._pending) or (
                self._active is not None and self._active.invocation_id == invocation.invocation_id
            ):
                raise RuntimeRefused("duplicate_invocation")
            self._pending.append(invocation)
            self._transition(RuntimeState.QUEUED, "waiting", invocation.variant, change_state=False)
            try:
                while self._active is not None or self._pending[0] is not invocation:
                    self._validate(invocation)
                    self._condition.wait(timeout=0.05)
                self._validate(invocation)
                if self.state is RuntimeState.QUARANTINED:
                    raise RuntimeRefused("runtime_quarantined")
                self._pending.popleft()
                self._active = invocation
            except BaseException:
                self._pending.remove(invocation)
                self._condition.notify_all()
                raise

    def _validate(self, invocation: RuntimeInvocation, *, residency: bool = False) -> None:
        if self._closed:
            raise RuntimeRefused("broker_closed")
        if invocation.cancellation.is_set():
            raise RuntimeRefused("cancelled")
        if not residency and invocation.deadline <= self._clock():
            raise RuntimeRefused("deadline_exceeded")
        try:
            authorized = self.authorize(invocation)
        except Exception as error:
            raise RuntimeRefused("authorization_unavailable") from error
        if invocation.authorized_until <= self._clock() or not authorized:
            raise RuntimeRefused("authorization_revoked")

    def _ensure_resident(self, invocation: RuntimeInvocation) -> None:
        resident = self._resident
        if resident is not None:
            self._validate(resident, residency=True)
            self._check_lease()
            same_scope = (
                resident.variant == invocation.variant
                and resident.policy_digest == invocation.policy_digest
                and resident.scope_digest == invocation.scope_digest
                and resident.authorization_digest == invocation.authorization_digest
                and resident.authorized_by == invocation.authorized_by
            )
            if same_scope and self.adapter.health(invocation.variant).ready_for(invocation.variant):
                return
            self._evict("variant_or_scope_changed")
        self._validate(invocation)
        self._lease = self.leases.create_lease(
            f"runtime-{uuid4().hex}",
            self.budget,
            invocation.variant.memory_mib,
            {"policy": invocation.policy_digest, "scope": invocation.scope_digest},
            invocation.authorized_by,
            invocation.authorization_digest,
            invocation_id=invocation.invocation_id,
            capability_digest=invocation.variant.digest,
            runtime_digest=invocation.variant.runtime_digest,
        )
        if self._lease.fence_token is None:
            raise RuntimeRefused("lease_invalid")
        self._lease = self.leases.activate_lease(
            self._lease.id, self._lease.fence_token, invocation.authorization_digest
        )
        self._resident = invocation
        self._transition(RuntimeState.LOADING, "cold_load", invocation.variant)
        self.adapter.load(invocation.variant, invocation.cancellation, invocation.deadline)
        self._validate(invocation)
        self._check_lease()
        if not self.adapter.health(invocation.variant).ready_for(invocation.variant):
            raise RuntimeRefused("runtime_not_ready")
        self._failures = 0
        self._transition(RuntimeState.WARM, "readiness_verified", invocation.variant)

    def _check_lease(self) -> None:
        if self._lease is None:
            raise RuntimeRefused("lease_invalid")
        current = self.leases.get_lease(self._lease.id)
        if (
            current.state is not LeaseState.ACTIVE
            or current.fence_token != self._lease.fence_token
            or current.expires_at is None
            or current.expires_at <= self._clock()
        ):
            raise RuntimeRefused("lease_invalid")

    def _evict(self, reason: str) -> None:
        resident = self._resident
        if resident is not None:
            self._transition(RuntimeState.EVICTING, reason, resident.variant)
            try:
                self.adapter.unload(resident.variant)
            except Exception as error:
                self._transition(RuntimeState.QUARANTINED, "unload_failed", resident.variant)
                raise RuntimeRefused("unload_failed") from error
        if self._lease is not None:
            lease = self.leases.get_lease(self._lease.id)
            if lease.fence_token is not None:
                if lease.state is LeaseState.ACTIVE:
                    self.leases.release_lease(lease.id, lease.fence_token)
                elif lease.state is LeaseState.RESERVED:
                    self.leases.expire_lease(lease.id, lease.fence_token)
        self._resident, self._lease, self._pinned = None, None, False
        self._transition(RuntimeState.COLD, reason, None)

    def _cleanup_after_failure(self, invocation: RuntimeInvocation, error: BaseException) -> None:
        failed_load = self._state is RuntimeState.LOADING
        self._evict("failed_or_cancelled")
        reason = error.reason_code if isinstance(error, RuntimeRefused) else "runtime_failed"
        if failed_load and reason not in {
            "cancelled",
            "deadline_exceeded",
            "authorization_revoked",
        }:
            self._failures += 1
        state = RuntimeState.CANCELLED if reason == "cancelled" else RuntimeState.FAILED
        if self._failures >= self._max_load_failures:
            state, reason = RuntimeState.QUARANTINED, "start_limit_exhausted"
        self._transition(state, reason, invocation.variant)

    def pin(self, variant_digest: str, *, pinned: bool = True) -> None:
        with self._condition:
            if self._resident is None or self._resident.variant.digest != variant_digest:
                raise RuntimeRefused("variant_not_resident")
            self._pinned = pinned

    def maintain(self) -> None:
        """Run from worker maintenance to enforce idle, policy and lease expiry."""
        with self._condition:
            if self._active is not None or self._resident is None:
                return
            try:
                self._validate(self._resident, residency=True)
                self._check_lease()
            except RuntimeRefused:
                self._evict("residency_expired")
                return
            if (
                not self._pinned
                and (self._clock() - self._last_used).total_seconds() >= self._idle_seconds
            ):
                self._evict("idle_eviction")

    def shutdown(self, *, timeout: float = 10) -> None:
        if not 0 <= timeout <= 600:
            raise ValueError("shutdown timeout must be bounded")
        until = monotonic() + timeout
        with self._condition:
            self._closed = True
            if self._active is not None:
                self._active.cancellation.set()
                self._transition(RuntimeState.DRAINING, "shutdown", self._active.variant)
            self._condition.notify_all()
            while self._active is not None:
                remaining = until - monotonic()
                if remaining <= 0:
                    raise RuntimeRefused("shutdown_timeout")
                self._condition.wait(timeout=min(remaining, 0.05))
            self._evict("shutdown")

    def reset(self) -> None:
        """Explicit recovery; never an automatic process-restart loop."""
        with self._condition:
            if self._active is not None or self._pending:
                raise RuntimeRefused("broker_busy")
            self._evict("operator_reset")
            self._failures, self._closed = 0, False

    def _transition(
        self,
        state: RuntimeState,
        reason: str,
        variant: RuntimeVariant | None,
        *,
        change_state: bool = True,
    ) -> None:
        with self._condition:
            if change_state:
                self._state = state
            self._sequence += 1
            transition = RuntimeTransition(
                self._sequence, state, reason, self._clock(), variant.digest if variant else None
            )
            self._history.append(transition)
        if self._telemetry is not None:
            self._telemetry.publish(
                TelemetrySignal.create(
                    kind=TelemetryKind.LIFECYCLE,
                    name="runtime.residency",
                    observed_at=transition.observed_at,
                    attributes={"state": state.value, "reason": reason},
                )
            )
