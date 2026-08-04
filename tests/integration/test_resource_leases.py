from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from af.domain.resources import (
    FenceToken,
    LeaseState,
    ResourceBudget,
    ResourceIdentity,
    ResourceKind,
)
from af.framework.errors import (
    LeaseBudgetExceeded,
    LeaseFenceTokenStale,
    LeaseNotFound,
    LeaseScopeWidened,
    LeaseStateTransitionError,
)

if TYPE_CHECKING:
    from af.bootstrap import AFApplication


@pytest.fixture
def budget() -> ResourceBudget:
    return ResourceBudget(
        identity=ResourceIdentity(
            kind=ResourceKind.CPU,
            host_id="test-host",
            instance_id="cpu-0",
        ),
        max_quantity=8.0,
        max_duration=timedelta(seconds=300),
        max_concurrent=4,
    )


@pytest.fixture
def scope() -> dict[str, str]:
    return {"host": "test-host", "zone": "us-east-1"}


class TestResourceLeaseServiceCreate:
    def test_create_lease_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        assert lease.id == lease_id
        assert lease.state == LeaseState.RESERVED
        assert lease.quantity == 2.0
        assert lease.fence_token is not None
        assert lease.fence_token.value == 1
        assert lease.authorized_by == "test-pipeline"

    def test_create_lease_returns_immutable_lease(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        with pytest.raises(FrozenInstanceError):
            lease.state = LeaseState.ACTIVE  # type: ignore[attr-defined]

    def test_create_lease_quantity_exceeds_budget_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())

        with pytest.raises(LeaseBudgetExceeded):
            application.leases.create_lease(
                lease_id=lease_id,
                budget=budget,
                quantity=100.0,
                scope=scope,
                authorized_by="test-pipeline",
                authorization_digest="abc123",
            )

    def test_create_lease_negative_quantity_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())

        with pytest.raises(LeaseBudgetExceeded):
            application.leases.create_lease(
                lease_id=lease_id,
                budget=budget,
                quantity=-1.0,
                scope=scope,
                authorized_by="test-pipeline",
                authorization_digest="abc123",
            )

    def test_create_lease_exact_budget_quantity_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=budget.max_quantity,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        assert lease.quantity == budget.max_quantity


class TestResourceLeaseServiceActivate:
    def test_activate_lease_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )

        assert active.state == LeaseState.ACTIVE
        assert active.fence_token.value == 2
        assert active.activated_at is not None
        assert active.expires_at is not None

    def test_activate_already_active_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )

        with pytest.raises(LeaseStateTransitionError):
            application.leases.activate_lease(
                lease_id=lease_id,
                fence_token=FenceToken(value=2, created_at=datetime.now(UTC)),
            )

    def test_activate_stale_fence_token_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        with pytest.raises(LeaseFenceTokenStale):
            application.leases.activate_lease(
                lease_id=lease_id,
                fence_token=FenceToken(value=0, created_at=datetime.now(UTC)),
            )

    def test_activate_nonexistent_lease_raises(self, application: AFApplication) -> None:
        lease_id = str(uuid.uuid4())

        with pytest.raises(LeaseNotFound):
            application.leases.activate_lease(
                lease_id=lease_id,
                fence_token=FenceToken(value=1, created_at=datetime.now(UTC)),
            )


class TestResourceLeaseServiceRenew:
    def test_renew_active_lease_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )

        renewed = application.leases.renew_lease(
            lease_id=lease_id,
            fence_token=active.fence_token,
        )

        assert renewed.state == LeaseState.ACTIVE
        assert renewed.fence_token.value == 3
        assert renewed.renewed_count == 1
        assert renewed.last_renewed_at is not None

    def test_renon_non_active_lease_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        with pytest.raises(LeaseStateTransitionError):
            application.leases.renew_lease(
                lease_id=lease_id,
                fence_token=lease.fence_token,
            )

    def test_renew_with_non_widening_scope_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )

        renewed = application.leases.renew_lease(
            lease_id=lease_id,
            fence_token=active.fence_token,
            new_scope=scope,
        )

        assert renewed.scope == scope

    def test_renew_with_widening_scope_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )

        with pytest.raises(LeaseScopeWidened):
            application.leases.renew_lease(
                lease_id=lease_id,
                fence_token=active.fence_token,
                new_scope={"host": "different-host", "zone": "us-east-1"},
            )


class TestResourceLeaseServiceRelease:
    def test_release_active_lease_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )

        released = application.leases.release_lease(
            lease_id=lease_id,
            fence_token=active.fence_token,
        )

        assert released.state == LeaseState.RELEASED
        assert released.fence_token.value == 3

    def test_release_reserved_lease_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        with pytest.raises(LeaseStateTransitionError):
            application.leases.release_lease(
                lease_id=lease_id,
                fence_token=lease.fence_token,
            )


class TestResourceLeaseServiceRevoke:
    def test_revoke_active_lease_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )

        revoked = application.leases.revoke_lease(
            lease_id=lease_id,
            fence_token=active.fence_token,
        )

        assert revoked.state == LeaseState.REVOKED
        assert revoked.fence_token.value == 3

    def test_revoke_reserved_lease_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        with pytest.raises(LeaseStateTransitionError):
            application.leases.revoke_lease(
                lease_id=lease_id,
                fence_token=lease.fence_token,
            )


class TestResourceLeaseServiceReconcile:
    def test_reconcile_released_lease_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )
        released = application.leases.release_lease(
            lease_id=lease_id,
            fence_token=active.fence_token,
        )

        reconciled = application.leases.reconcile_lease(
            lease_id=lease_id,
            fence_token=released.fence_token,
        )

        assert reconciled.state == LeaseState.RECONCILED
        assert reconciled.fence_token.value == 4

    def test_reconcile_revoked_lease_succeeds(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )
        revoked = application.leases.revoke_lease(
            lease_id=lease_id,
            fence_token=active.fence_token,
        )

        reconciled = application.leases.reconcile_lease(
            lease_id=lease_id,
            fence_token=revoked.fence_token,
        )

        assert reconciled.state == LeaseState.RECONCILED

    def test_reconcile_active_lease_raises(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        lease = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=lease.fence_token,
        )

        with pytest.raises(LeaseStateTransitionError):
            application.leases.reconcile_lease(
                lease_id=lease_id,
                fence_token=active.fence_token,
            )


class TestResourceLeaseServiceGet:
    def test_get_lease_returns_existing(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())
        original = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )

        fetched = application.leases.get_lease(lease_id)

        assert fetched.id == original.id
        assert fetched.state == original.state

    def test_get_nonexistent_lease_raises(self, application: AFApplication) -> None:
        lease_id = str(uuid.uuid4())

        with pytest.raises(LeaseNotFound):
            application.leases.get_lease(lease_id)


class TestFullLeaseLifecycle:
    def test_reserve_activate_release_reconcile(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())

        reserved = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        assert reserved.state == LeaseState.RESERVED
        assert reserved.fence_token.value == 1

        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=reserved.fence_token,
        )
        assert active.state == LeaseState.ACTIVE
        assert active.fence_token.value == 2

        released = application.leases.release_lease(
            lease_id=lease_id,
            fence_token=active.fence_token,
        )
        assert released.state == LeaseState.RELEASED
        assert released.fence_token.value == 3

        reconciled = application.leases.reconcile_lease(
            lease_id=lease_id,
            fence_token=released.fence_token,
        )
        assert reconciled.state == LeaseState.RECONCILED
        assert reconciled.fence_token.value == 4

    def test_reserve_revoke_reconcile(
        self, application: AFApplication, budget: ResourceBudget, scope: dict[str, str]
    ) -> None:
        lease_id = str(uuid.uuid4())

        reserved = application.leases.create_lease(
            lease_id=lease_id,
            budget=budget,
            quantity=2.0,
            scope=scope,
            authorized_by="test-pipeline",
            authorization_digest="abc123",
        )
        assert reserved.state == LeaseState.RESERVED

        # RESERVED can transition to ACTIVE or EXPIRED, not directly to REVOKED
        # So we need to go through ACTIVE first
        active = application.leases.activate_lease(
            lease_id=lease_id,
            fence_token=reserved.fence_token,
        )

        revoked = application.leases.revoke_lease(
            lease_id=lease_id,
            fence_token=active.fence_token,
        )
        assert revoked.state == LeaseState.REVOKED
        assert revoked.fence_token.value == 3

        reconciled = application.leases.reconcile_lease(
            lease_id=lease_id,
            fence_token=revoked.fence_token,
        )
        assert reconciled.state == LeaseState.RECONCILED
        assert reconciled.fence_token.value == 4
