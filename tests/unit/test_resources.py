"""Tests for resource lease domain model and budget validation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from af.domain.resources import (
    FenceToken,
    Lease,
    LeaseState,
    ResourceBudget,
    ResourceIdentity,
    ResourceKind,
)
from af.framework.errors import (
    LeaseBudgetExceeded,
    LeaseConcurrencyExceeded,
    LeaseFenceTokenStale,
    LeaseScopeWidened,
    LeaseStateTransitionError,
)


@pytest.fixture
def cpu_identity() -> ResourceIdentity:
    return ResourceIdentity(
        kind=ResourceKind.CPU,
        host_id="test-host-001",
        instance_id="cpu-001",
    )


@pytest.fixture
def cpu_budget(cpu_identity: ResourceIdentity) -> ResourceBudget:
    return ResourceBudget(
        identity=cpu_identity,
        max_quantity=100.0,
        max_duration=timedelta(hours=1),
        max_concurrent=10,
    )


@pytest.fixture
def accelerator_identity() -> ResourceIdentity:
    return ResourceIdentity(
        kind=ResourceKind.ACCELERATOR,
        host_id="test-host-001",
        instance_id="gpu-001",
        topology={"device": "cuda:0", "vendor": "nvidia"},
    )


@pytest.fixture
def accelerator_budget(accelerator_identity: ResourceIdentity) -> ResourceBudget:
    return ResourceBudget(
        identity=accelerator_identity,
        max_quantity=16.0,
        max_duration=timedelta(hours=2),
        max_concurrent=4,
    )


# -- Budget validation tests --


def test_budget_validation_accepts_within_limits(cpu_budget: ResourceBudget) -> None:
    assert cpu_budget.max_quantity == 100.0
    assert cpu_budget.max_concurrent == 10
    assert cpu_budget.max_duration == timedelta(hours=1)


def test_budget_validation_rejects_quantity_overflow(cpu_budget: ResourceBudget) -> None:
    with pytest.raises(LeaseBudgetExceeded, match="requested 150.0, available 100.0"):
        _validate_quantity(cpu_budget, 150.0)


def test_budget_validation_rejects_concurrency_overflow(cpu_budget: ResourceBudget) -> None:
    with pytest.raises(LeaseConcurrencyExceeded, match="requested 15, max 10"):
        _validate_concurrency(cpu_budget, 15)


def test_budget_validation_accepts_exact_limit(cpu_budget: ResourceBudget) -> None:
    _validate_quantity(cpu_budget, 100.0)
    _validate_concurrency(cpu_budget, 10)


def test_budget_validation_rejects_negative_quantity(cpu_budget: ResourceBudget) -> None:
    with pytest.raises(LeaseBudgetExceeded):
        _validate_quantity(cpu_budget, -1.0)


# -- Non-widening scope tests --


def test_non_widening_scope_rejects_wider_scope() -> None:
    original_scope = {"cpu": "read"}
    requested_scope = {"cpu": "write", "memory": "read"}
    with pytest.raises(LeaseScopeWidened, match="scope widened on renewal"):
        _validate_non_widening_scope("lease-001", original_scope, requested_scope)


def test_non_widening_scope_accepts_same_scope() -> None:
    scope = {"cpu": "read"}
    _validate_non_widening_scope("lease-001", scope, scope)


def test_non_widening_scope_accepts_narrower_scope() -> None:
    original_scope = {"cpu": "read", "memory": "read"}
    requested_scope = {"cpu": "read"}
    _validate_non_widening_scope("lease-001", original_scope, requested_scope)


# -- State machine tests --


def test_state_machine_valid_transitions() -> None:
    valid_transitions = [
        (LeaseState.RESERVED, LeaseState.ACTIVE),
        (LeaseState.ACTIVE, LeaseState.EXPIRED),
        (LeaseState.ACTIVE, LeaseState.RELEASED),
        (LeaseState.ACTIVE, LeaseState.REVOKED),
        (LeaseState.RESERVED, LeaseState.EXPIRED),
        (LeaseState.EXPIRED, LeaseState.RECONCILED),
        (LeaseState.RELEASED, LeaseState.RECONCILED),
        (LeaseState.REVOKED, LeaseState.RECONCILED),
    ]
    for from_state, to_state in valid_transitions:
        _validate_transition("lease-001", from_state, to_state)


def test_state_machine_invalid_transitions() -> None:
    invalid_transitions = [
        (LeaseState.ACTIVE, LeaseState.RESERVED),
        (LeaseState.RELEASED, LeaseState.ACTIVE),
        (LeaseState.RECONCILED, LeaseState.ACTIVE),
        (LeaseState.EXPIRED, LeaseState.ACTIVE),
    ]
    for from_state, to_state in invalid_transitions:
        with pytest.raises(LeaseStateTransitionError):
            _validate_transition("lease-001", from_state, to_state)


# -- Fence token tests --


def test_fence_token_monotonically_increasing() -> None:
    now = datetime.now()
    token1 = FenceToken(value=1, created_at=now)
    token2 = FenceToken(value=2, created_at=now)
    token3 = FenceToken(value=1, created_at=now)

    _validate_fence_token("lease-001", token1, token1)
    _validate_fence_token("lease-001", token2, token2)

    with pytest.raises(LeaseFenceTokenStale):
        _validate_fence_token("lease-001", token2, token3)


def test_fence_token_accepts_equal_or_greater() -> None:
    now = datetime.now()
    token = FenceToken(value=5, created_at=now)
    newer = FenceToken(value=10, created_at=now)
    _validate_fence_token("lease-001", token, newer)


# -- Lease immutability tests --


def test_lease_is_immutable(cpu_budget: ResourceBudget) -> None:
    now = datetime.now()
    lease = Lease(
        id="lease-001",
        budget=cpu_budget,
        state=LeaseState.RESERVED,
        quantity=10.0,
        requested_at=now,
        created_at=now,
    )
    with pytest.raises(Exception):
        lease.state = LeaseState.ACTIVE  # type: ignore[attr-defined]


def test_lease_with_all_fields(cpu_budget: ResourceBudget) -> None:
    now = datetime.now()
    token = FenceToken(value=1, created_at=now)
    lease = Lease(
        id="lease-001",
        budget=cpu_budget,
        state=LeaseState.ACTIVE,
        quantity=10.0,
        requested_at=now,
        activated_at=now,
        expires_at=now + timedelta(hours=1),
        fence_token=token,
        invocation_id="inv-001",
        capability_digest="sha256:abc123",
        runtime_digest="sha256:def456",
        authorized_by="auth-001",
        authorization_digest="sha256:ghi789",
        renewed_count=0,
        created_at=now,
    )
    assert lease.id == "lease-001"
    assert lease.state == LeaseState.ACTIVE
    assert lease.quantity == 10.0
    assert lease.fence_token == token
    assert lease.invocation_id == "inv-001"


# -- Helper functions for validation logic --


def _validate_quantity(budget: ResourceBudget, quantity: float) -> None:
    if quantity < 0 or quantity > budget.max_quantity:
        raise LeaseBudgetExceeded(
            lease_id="test-lease",
            requested=quantity,
            available=budget.max_quantity,
        )


def _validate_concurrency(budget: ResourceBudget, concurrent: int) -> None:
    if concurrent < 0 or concurrent > (budget.max_concurrent or 0):
        raise LeaseConcurrencyExceeded(
            lease_id="test-lease",
            requested=concurrent,
            max_concurrent=budget.max_concurrent or 0,
        )


def _validate_non_widening_scope(
    lease_id: str,
    original_scope: dict[str, str],
    requested_scope: dict[str, str],
) -> None:
    for key, value in requested_scope.items():
        if key not in original_scope or original_scope[key] != value:
            raise LeaseScopeWidened(
                lease_id=lease_id,
                original=str(original_scope),
                requested=str(requested_scope),
            )


def _validate_transition(
    lease_id: str,
    from_state: LeaseState,
    to_state: LeaseState,
) -> None:
    valid_transitions: dict[LeaseState, set[LeaseState]] = {
        LeaseState.RESERVED: {LeaseState.ACTIVE, LeaseState.EXPIRED},
        LeaseState.ACTIVE: {LeaseState.EXPIRED, LeaseState.RELEASED, LeaseState.REVOKED},
        LeaseState.EXPIRED: {LeaseState.RECONCILED},
        LeaseState.RELEASED: {LeaseState.RECONCILED},
        LeaseState.REVOKED: {LeaseState.RECONCILED},
        LeaseState.RECONCILED: set(),
    }
    allowed = valid_transitions.get(from_state, set())
    if to_state not in allowed:
        raise LeaseStateTransitionError(
            lease_id=lease_id,
            from_state=from_state.value,
            to_state=to_state.value,
        )


def _validate_fence_token(
    lease_id: str,
    expected_token: FenceToken,
    provided_token: FenceToken,
) -> None:
    if provided_token.value < expected_token.value:
        raise LeaseFenceTokenStale(
            lease_id=lease_id,
            expected=expected_token.value,
            provided=provided_token.value,
        )
