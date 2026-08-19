"""Application service for resource lease lifecycle management."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from math import isfinite
from threading import RLock

from af.domain.resources import (
    FenceToken,
    Lease,
    LeaseAdmissionStatus,
    LeaseRepository,
    LeaseState,
    ResourceBudget,
)
from af.framework.errors import (
    LeaseAuthorizationExpired,
    LeaseBudgetExceeded,
    LeaseConcurrencyExceeded,
    LeaseFenceTokenStale,
    LeaseNotFound,
    LeaseScopeWidened,
    LeaseStateTransitionError,
)
from af.observability.signals import TelemetryKind, TelemetryPublisher, TelemetrySignal

# Valid state transitions
_VALID_TRANSITIONS: dict[LeaseState, set[LeaseState]] = {
    LeaseState.RESERVED: {LeaseState.ACTIVE, LeaseState.EXPIRED},
    LeaseState.ACTIVE: {LeaseState.EXPIRED, LeaseState.RELEASED, LeaseState.REVOKED},
    LeaseState.EXPIRED: {LeaseState.RECONCILED},
    LeaseState.RELEASED: {LeaseState.RECONCILED},
    LeaseState.REVOKED: {LeaseState.RECONCILED},
    LeaseState.RECONCILED: set(),
}


@dataclass(frozen=True, slots=True)
class LeaseSweepResult:
    """Deterministic evidence from one expired-lease reconciliation pass."""

    expired_lease_ids: tuple[str, ...]
    reconciled_lease_ids: tuple[str, ...]
    conflicted_lease_ids: tuple[str, ...]


def _count_bucket(items: tuple[str, ...]) -> str:
    if not items:
        return "none"
    if len(items) == 1:
        return "one"
    return "multiple"


class ResourceLeaseSweepMaintenance:
    """Expose resource reconciliation as observable synchronous worker maintenance."""

    def __init__(
        self,
        service: ResourceLeaseService,
        *,
        host_id: str | None = None,
        telemetry: TelemetryPublisher | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(service, ResourceLeaseService):
            raise TypeError("resource lease maintenance requires a ResourceLeaseService")
        if host_id is not None and (type(host_id) is not str or not host_id or "\n" in host_id):
            raise ValueError("resource lease maintenance host identity is invalid")
        if telemetry is not None and not isinstance(telemetry, TelemetryPublisher):
            raise TypeError("resource lease maintenance telemetry publisher is invalid")
        if not callable(clock):
            raise TypeError("resource lease maintenance clock must be callable")
        self._service = service
        self._host_id = host_id
        self._telemetry = telemetry
        self._clock = clock
        self._run_count = 0
        self._last_result: LeaseSweepResult | None = None
        self._last_completed_at: datetime | None = None
        self._lock = RLock()

    @property
    def run_count(self) -> int:
        with self._lock:
            return self._run_count

    @property
    def last_result(self) -> LeaseSweepResult | None:
        with self._lock:
            return self._last_result

    @property
    def last_completed_at(self) -> datetime | None:
        with self._lock:
            return self._last_completed_at

    def run(self) -> None:
        """Run one sweep and retain exact local evidence for operator inspection."""

        try:
            result = self._service.sweep_expired_leases(host_id=self._host_id)
        except BaseException:
            self._publish(None, observed_at=self._clock())
            raise
        completed_at = self._clock()
        with self._lock:
            self._run_count += 1
            self._last_result = result
            self._last_completed_at = completed_at
        self._publish(result, observed_at=completed_at)

    def _publish(
        self,
        result: LeaseSweepResult | None,
        *,
        observed_at: datetime,
    ) -> None:
        publisher = self._telemetry
        if publisher is None:
            return
        if result is None:
            attributes = {
                "conflicted": "unknown",
                "expired": "unknown",
                "reconciled": "unknown",
                "status": "failed",
            }
        else:
            status = (
                "conflicted"
                if result.conflicted_lease_ids
                else "reconciled"
                if result.expired_lease_ids or result.reconciled_lease_ids
                else "clean"
            )
            attributes = {
                "conflicted": _count_bucket(result.conflicted_lease_ids),
                "expired": _count_bucket(result.expired_lease_ids),
                "reconciled": _count_bucket(result.reconciled_lease_ids),
                "status": status,
            }
        try:
            publisher.publish(
                TelemetrySignal.create(
                    kind=TelemetryKind.METRIC,
                    name="resource.lease.sweep",
                    observed_at=observed_at,
                    attributes=attributes,
                )
            )
        except Exception:
            return


class ResourceLeaseService:
    """Application service managing resource lease lifecycle."""

    def __init__(
        self,
        lease_repository: LeaseRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not callable(clock):
            raise TypeError("resource lease clock must be callable")
        self._repository = lease_repository
        self._clock = clock

    # -- Public API --

    def create_lease(
        self,
        lease_id: str,
        budget: ResourceBudget,
        quantity: float,
        scope: dict[str, str],
        authorized_by: str,
        authorization_digest: str,
        invocation_id: str | None = None,
        capability_digest: str | None = None,
        runtime_digest: str | None = None,
    ) -> Lease:
        """Create a new lease in RESERVED state."""
        self._validate_quantity(budget, quantity)

        now = self._clock()
        token = FenceToken(value=1, created_at=now)

        lease = Lease(
            id=lease_id,
            budget=budget,
            state=LeaseState.RESERVED,
            quantity=quantity,
            requested_at=now,
            created_at=now,
            expires_at=(now + budget.max_duration if budget.max_duration is not None else None),
            fence_token=token,
            invocation_id=invocation_id,
            capability_digest=capability_digest,
            runtime_digest=runtime_digest,
            authorized_by=authorized_by,
            authorization_digest=authorization_digest,
            scope=scope,
            renewed_count=0,
        )

        admission = self._repository.reserve(lease)
        if admission.status is LeaseAdmissionStatus.CAPACITY_EXCEEDED:
            raise LeaseBudgetExceeded(
                lease_id=lease_id,
                requested=quantity,
                available=admission.available_quantity,
            )
        if admission.status is LeaseAdmissionStatus.CONCURRENCY_EXCEEDED:
            if admission.max_concurrent is None:
                raise AssertionError("concurrency rejection requires an effective limit")
            raise LeaseConcurrencyExceeded(
                lease_id=lease_id,
                requested=admission.concurrent + 1,
                max_concurrent=admission.max_concurrent,
            )
        if admission.status is LeaseAdmissionStatus.DUPLICATE:
            current = self._repository.get(lease_id)
            expected = (
                current.fence_token.value
                if current is not None and current.fence_token is not None
                else 1
            )
            raise LeaseFenceTokenStale(
                lease_id=lease_id,
                expected=expected,
                provided=0,
            )
        if admission.status is not LeaseAdmissionStatus.ADMITTED:
            raise AssertionError(f"unhandled lease admission status: {admission.status}")
        return lease

    def activate_lease(
        self,
        lease_id: str,
        fence_token: FenceToken,
        authorization_digest: str | None = None,
    ) -> Lease:
        """Transition a lease from RESERVED to ACTIVE."""
        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)
        self._validate_transition(lease, LeaseState.ACTIVE)

        if authorization_digest:
            self._validate_authorization(lease, authorization_digest)

        now = self._clock()
        if self._is_due(lease, now):
            raise LeaseStateTransitionError(
                lease_id=lease_id,
                from_state=lease.state.value,
                to_state=LeaseState.EXPIRED.value,
            )
        updated = replace(
            lease,
            state=LeaseState.ACTIVE,
            activated_at=now,
            expires_at=(
                now + lease.budget.max_duration if lease.budget.max_duration is not None else None
            ),
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
            authorization_digest=authorization_digest or lease.authorization_digest,
        )
        self._persist_transition(updated, expected_fence=fence_token.value)
        return updated

    def renew_lease(
        self,
        lease_id: str,
        fence_token: FenceToken,
        new_scope: dict[str, str] | None = None,
        authorization_digest: str | None = None,
    ) -> Lease:
        """Renew an active lease, extending its expiration."""
        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)

        if authorization_digest:
            self._validate_authorization(lease, authorization_digest)

        if lease.state != LeaseState.ACTIVE:
            raise LeaseStateTransitionError(
                lease_id=lease_id,
                from_state=lease.state.value,
                to_state=LeaseState.ACTIVE.value,
            )

        now = self._clock()
        if lease.expires_at is not None and lease.expires_at <= now:
            raise LeaseStateTransitionError(
                lease_id=lease_id,
                from_state=lease.state.value,
                to_state=LeaseState.EXPIRED.value,
            )

        if new_scope:
            self._validate_non_widening_scope(lease, new_scope)

        updated = replace(
            lease,
            state=LeaseState.ACTIVE,
            expires_at=(
                now + lease.budget.max_duration if lease.budget.max_duration is not None else None
            ),
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
            scope=new_scope or lease.scope,
            renewed_count=lease.renewed_count + 1,
            last_renewed_at=now,
        )
        self._persist_transition(updated, expected_fence=fence_token.value)
        return updated

    def release_lease(
        self,
        lease_id: str,
        fence_token: FenceToken,
    ) -> Lease:
        """Release an active lease."""
        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)
        self._validate_transition(lease, LeaseState.RELEASED)

        now = self._clock()
        updated = replace(
            lease,
            state=LeaseState.RELEASED,
            released_at=now,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
        )
        self._persist_transition(updated, expected_fence=fence_token.value)
        return updated

    def expire_lease(self, lease_id: str, fence_token: FenceToken) -> Lease:
        """Fence and expire reserved or active capacity after failed acquisition or timeout."""

        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)
        now = self._clock()
        updated = self._expired_transition(lease, fence_token, now)
        self._persist_transition(updated, expected_fence=fence_token.value)
        return updated

    def revoke_lease(
        self,
        lease_id: str,
        fence_token: FenceToken,
    ) -> Lease:
        """Revoke an active lease."""
        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)
        self._validate_transition(lease, LeaseState.REVOKED)

        now = self._clock()
        updated = replace(
            lease,
            state=LeaseState.REVOKED,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
        )
        self._persist_transition(updated, expected_fence=fence_token.value)
        return updated

    def reconcile_lease(
        self,
        lease_id: str,
        fence_token: FenceToken,
    ) -> Lease:
        """Transition a terminal lease to RECONCILED."""
        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)

        now = self._clock()
        updated = self._reconciled_transition(lease, fence_token, now)
        self._persist_transition(updated, expected_fence=fence_token.value)
        return updated

    def sweep_expired_leases(self, *, host_id: str | None = None) -> LeaseSweepResult:
        """Fence due capacity and finish persisted expiry transitions after a restart."""

        now = self._clock()
        snapshot = sorted(
            self._repository.list(host_id=host_id),
            key=lambda lease: (lease.created_at, lease.id),
        )
        expired: list[Lease] = []
        reconcile: list[Lease] = [lease for lease in snapshot if lease.state is LeaseState.EXPIRED]
        conflicts: list[str] = []

        for lease in snapshot:
            if lease.state not in {LeaseState.RESERVED, LeaseState.ACTIVE}:
                continue
            if not self._is_due(lease, now):
                continue
            token = lease.fence_token
            if token is None:
                conflicts.append(lease.id)
                continue
            updated = self._expired_transition(lease, token, now)
            if not self._repository.compare_and_swap(updated, expected_fence=token.value):
                conflicts.append(lease.id)
                continue
            expired.append(updated)
            reconcile.append(updated)

        reconciled: list[Lease] = []
        for lease in reconcile:
            token = lease.fence_token
            if token is None:
                if lease.id not in conflicts:
                    conflicts.append(lease.id)
                continue
            updated = self._reconciled_transition(lease, token, now)
            if not self._repository.compare_and_swap(updated, expected_fence=token.value):
                if lease.id not in conflicts:
                    conflicts.append(lease.id)
                continue
            reconciled.append(updated)

        return LeaseSweepResult(
            expired_lease_ids=tuple(lease.id for lease in expired),
            reconciled_lease_ids=tuple(lease.id for lease in reconciled),
            conflicted_lease_ids=tuple(conflicts),
        )

    def get_lease(self, lease_id: str) -> Lease:
        """Get a lease by ID."""
        return self._get_lease(lease_id)

    # -- Private helpers --

    def _get_lease(self, lease_id: str) -> Lease:
        lease = self._repository.get(lease_id)
        if lease is None:
            raise LeaseNotFound(lease_id=lease_id)
        return lease

    def _validate_quantity(self, budget: ResourceBudget, quantity: float) -> None:
        if (
            not isfinite(budget.max_quantity)
            or not isfinite(quantity)
            or quantity < 0
            or quantity > budget.max_quantity
        ):
            raise LeaseBudgetExceeded(
                lease_id="pending",
                requested=quantity,
                available=budget.max_quantity,
            )

    def _validate_concurrency(self, budget: ResourceBudget, concurrent: int) -> None:
        if concurrent < 0 or concurrent > (budget.max_concurrent or 0):
            raise LeaseConcurrencyExceeded(
                lease_id="pending",
                requested=concurrent,
                max_concurrent=budget.max_concurrent or 0,
            )

    @staticmethod
    def _is_due(lease: Lease, now: datetime) -> bool:
        expiry = lease.expires_at
        if expiry is None and lease.state is LeaseState.RESERVED:
            duration = lease.budget.max_duration
            if duration is not None:
                expiry = lease.created_at + duration
        return expiry is not None and expiry <= now

    def _expired_transition(
        self,
        lease: Lease,
        fence_token: FenceToken,
        now: datetime,
    ) -> Lease:
        self._validate_transition(lease, LeaseState.EXPIRED)
        return replace(
            lease,
            state=LeaseState.EXPIRED,
            expires_at=now,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
        )

    def _reconciled_transition(
        self,
        lease: Lease,
        fence_token: FenceToken,
        now: datetime,
    ) -> Lease:
        self._validate_transition(lease, LeaseState.RECONCILED)
        return replace(
            lease,
            state=LeaseState.RECONCILED,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
        )

    def _validate_non_widening_scope(
        self,
        lease: Lease,
        requested_scope: dict[str, str],
    ) -> None:
        original_scope = lease.scope or {}
        for key, value in requested_scope.items():
            if key not in original_scope or original_scope[key] != value:
                raise LeaseScopeWidened(
                    lease_id=lease.id,
                    original=str(original_scope),
                    requested=str(requested_scope),
                )

    def _validate_transition(
        self,
        lease: Lease,
        to_state: LeaseState,
    ) -> None:
        allowed = _VALID_TRANSITIONS.get(lease.state, set())
        if to_state not in allowed:
            raise LeaseStateTransitionError(
                lease_id=lease.id,
                from_state=lease.state.value,
                to_state=to_state.value,
            )

    def _validate_fence_token(
        self,
        lease: Lease,
        provided_token: FenceToken,
    ) -> None:
        expected = lease.fence_token
        if expected and provided_token.value != expected.value:
            raise LeaseFenceTokenStale(
                lease_id=lease.id,
                expected=expected.value,
                provided=provided_token.value,
            )

    def _validate_authorization(
        self,
        lease: Lease,
        authorization_digest: str,
    ) -> None:
        if lease.authorization_digest and lease.authorization_digest != authorization_digest:
            raise LeaseAuthorizationExpired(
                lease_id=lease.id,
                authorized_by=lease.authorization_digest or "unknown",
            )

    def _persist_transition(self, lease: Lease, *, expected_fence: int) -> None:
        if self._repository.compare_and_swap(lease, expected_fence=expected_fence):
            return
        current = self._repository.get(lease.id)
        current_fence = (
            current.fence_token.value
            if current is not None and current.fence_token is not None
            else expected_fence
        )
        raise LeaseFenceTokenStale(
            lease_id=lease.id,
            expected=current_fence,
            provided=expected_fence,
        )
