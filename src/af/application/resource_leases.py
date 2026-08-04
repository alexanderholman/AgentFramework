"""Application service for resource lease lifecycle management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from af.domain.resources import (
    FenceToken,
    Lease,
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


# Valid state transitions
_VALID_TRANSITIONS: dict[LeaseState, set[LeaseState]] = {
    LeaseState.RESERVED: {LeaseState.ACTIVE, LeaseState.EXPIRED},
    LeaseState.ACTIVE: {LeaseState.EXPIRED, LeaseState.RELEASED, LeaseState.REVOKED},
    LeaseState.EXPIRED: {LeaseState.RECONCILED},
    LeaseState.RELEASED: {LeaseState.RECONCILED},
    LeaseState.REVOKED: {LeaseState.RECONCILED},
    LeaseState.RECONCILED: set(),
}


class ResourceLeaseService:
    """Application service managing resource lease lifecycle."""

    def __init__(self, lease_repository: LeaseRepository) -> None:
        self._repository = lease_repository

    # -- Public API --

    def create_lease(
        self,
        lease_id: str,
        budget: ResourceBudget,
        quantity: float,
        scope: dict[str, str],
        authorized_by: str,
        authorization_digest: str,
        invocation_id: Optional[str] = None,
        capability_digest: Optional[str] = None,
        runtime_digest: Optional[str] = None,
    ) -> Lease:
        """Create a new lease in RESERVED state."""
        self._validate_quantity(budget, quantity)

        now = datetime.now(timezone.utc)
        token = FenceToken(value=1, created_at=now)

        lease = Lease(
            id=lease_id,
            budget=budget,
            state=LeaseState.RESERVED,
            quantity=quantity,
            requested_at=now,
            created_at=now,
            fence_token=token,
            invocation_id=invocation_id,
            capability_digest=capability_digest,
            runtime_digest=runtime_digest,
            authorized_by=authorized_by,
            authorization_digest=authorization_digest,
            scope=scope,
            renewed_count=0,
        )

        self._repository.save(lease)
        return lease

    def activate_lease(
        self,
        lease_id: str,
        fence_token: FenceToken,
        authorization_digest: Optional[str] = None,
    ) -> Lease:
        """Transition a lease from RESERVED to ACTIVE."""
        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)
        self._validate_transition(lease, LeaseState.ACTIVE)

        if authorization_digest:
            self._validate_authorization(lease, authorization_digest)

        now = datetime.now(timezone.utc)
        updated = Lease(
            id=lease.id,
            budget=lease.budget,
            state=LeaseState.ACTIVE,
            quantity=lease.quantity,
            requested_at=lease.requested_at,
            activated_at=now,
            expires_at=now + lease.budget.max_duration,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
            invocation_id=lease.invocation_id,
            capability_digest=lease.capability_digest,
            runtime_digest=lease.runtime_digest,
            authorized_by=lease.authorized_by,
            authorization_digest=authorization_digest or lease.authorization_digest,
            scope=lease.scope,
            renewed_count=lease.renewed_count,
            created_at=lease.created_at,
        )

        self._repository.save(updated)
        return updated

    def renew_lease(
        self,
        lease_id: str,
        fence_token: FenceToken,
        new_scope: Optional[dict[str, str]] = None,
    ) -> Lease:
        """Renew an active lease, extending its expiration."""
        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)

        if lease.state != LeaseState.ACTIVE:
            raise LeaseStateTransitionError(
                lease_id=lease_id,
                from_state=lease.state.value,
                to_state=LeaseState.ACTIVE.value,
            )

        if new_scope:
            self._validate_non_widening_scope(lease, new_scope)

        now = datetime.now(timezone.utc)
        updated = Lease(
            id=lease.id,
            budget=lease.budget,
            state=LeaseState.ACTIVE,
            quantity=lease.quantity,
            requested_at=lease.requested_at,
            activated_at=lease.activated_at,
            expires_at=now + lease.budget.max_duration,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
            invocation_id=lease.invocation_id,
            capability_digest=lease.capability_digest,
            runtime_digest=lease.runtime_digest,
            authorized_by=lease.authorized_by,
            authorization_digest=lease.authorization_digest,
            scope=new_scope or lease.scope,
            renewed_count=lease.renewed_count + 1,
            last_renewed_at=now,
            created_at=lease.created_at,
        )

        self._repository.save(updated)
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

        now = datetime.now(timezone.utc)
        updated = Lease(
            id=lease.id,
            budget=lease.budget,
            state=LeaseState.RELEASED,
            quantity=lease.quantity,
            requested_at=lease.requested_at,
            activated_at=lease.activated_at,
            expires_at=lease.expires_at,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
            invocation_id=lease.invocation_id,
            capability_digest=lease.capability_digest,
            runtime_digest=lease.runtime_digest,
            authorized_by=lease.authorized_by,
            authorization_digest=lease.authorization_digest,
            scope=lease.scope,
            renewed_count=lease.renewed_count,
            created_at=lease.created_at,
        )

        self._repository.save(updated)
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

        now = datetime.now(timezone.utc)
        updated = Lease(
            id=lease.id,
            budget=lease.budget,
            state=LeaseState.REVOKED,
            quantity=lease.quantity,
            requested_at=lease.requested_at,
            activated_at=lease.activated_at,
            expires_at=lease.expires_at,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
            invocation_id=lease.invocation_id,
            capability_digest=lease.capability_digest,
            runtime_digest=lease.runtime_digest,
            authorized_by=lease.authorized_by,
            authorization_digest=lease.authorization_digest,
            scope=lease.scope,
            renewed_count=lease.renewed_count,
            created_at=lease.created_at,
        )

        self._repository.save(updated)
        return updated

    def reconcile_lease(
        self,
        lease_id: str,
        fence_token: FenceToken,
    ) -> Lease:
        """Transition a terminal lease to RECONCILED."""
        lease = self._get_lease(lease_id)
        self._validate_fence_token(lease, fence_token)
        self._validate_transition(lease, LeaseState.RECONCILED)

        now = datetime.now(timezone.utc)
        updated = Lease(
            id=lease.id,
            budget=lease.budget,
            state=LeaseState.RECONCILED,
            quantity=lease.quantity,
            requested_at=lease.requested_at,
            activated_at=lease.activated_at,
            expires_at=lease.expires_at,
            fence_token=FenceToken(value=fence_token.value + 1, created_at=now),
            invocation_id=lease.invocation_id,
            capability_digest=lease.capability_digest,
            runtime_digest=lease.runtime_digest,
            authorized_by=lease.authorized_by,
            authorization_digest=lease.authorization_digest,
            scope=lease.scope,
            renewed_count=lease.renewed_count,
            created_at=lease.created_at,
        )

        self._repository.save(updated)
        return updated

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
        if quantity < 0 or quantity > budget.max_quantity:
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
        if expected and provided_token.value < expected.value:
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
                expected=lease.authorization_digest,
                provided=authorization_digest,
            )
