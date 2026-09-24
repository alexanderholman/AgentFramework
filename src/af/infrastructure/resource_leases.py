"""In-memory reference driver for LeaseRepository."""

from __future__ import annotations

from threading import RLock

from af.domain.resources import (
    Lease,
    LeaseAdmissionResult,
    LeaseAdmissionStatus,
    LeaseRepository,
    LeaseState,
)

_CAPACITY_CONSUMING_STATES = frozenset({LeaseState.RESERVED, LeaseState.ACTIVE})


class InMemoryLeaseRepository(LeaseRepository):
    """In-memory implementation of LeaseRepository for testing."""

    def __init__(self) -> None:
        self._store: dict[str, Lease] = {}
        self._lock = RLock()

    def save(self, lease: Lease) -> None:
        with self._lock:
            self._store[lease.id] = lease

    def reserve(self, lease: Lease) -> LeaseAdmissionResult:
        """Atomically admit a lease against aggregate identity limits."""

        with self._lock:
            consuming = [
                current
                for current in self._store.values()
                if current.budget.identity == lease.budget.identity
                and current.state in _CAPACITY_CONSUMING_STATES
            ]
            used_quantity = sum(current.quantity for current in consuming)
            max_quantity = min(
                (
                    lease.budget.max_quantity,
                    *(current.budget.max_quantity for current in consuming),
                )
            )
            concurrency_limits = [
                limit
                for limit in (
                    lease.budget.max_concurrent,
                    *(current.budget.max_concurrent for current in consuming),
                )
                if limit is not None
            ]
            max_concurrent = min(concurrency_limits) if concurrency_limits else None
            available_quantity = max(max_quantity - used_quantity, 0.0)
            concurrent = len(consuming)

            if lease.id in self._store:
                return LeaseAdmissionResult(
                    status=LeaseAdmissionStatus.DUPLICATE,
                    available_quantity=available_quantity,
                    concurrent=concurrent,
                    max_concurrent=max_concurrent,
                )
            if lease.quantity > available_quantity:
                return LeaseAdmissionResult(
                    status=LeaseAdmissionStatus.CAPACITY_EXCEEDED,
                    available_quantity=available_quantity,
                    concurrent=concurrent,
                    max_concurrent=max_concurrent,
                )
            if max_concurrent is not None and concurrent >= max_concurrent:
                return LeaseAdmissionResult(
                    status=LeaseAdmissionStatus.CONCURRENCY_EXCEEDED,
                    available_quantity=available_quantity,
                    concurrent=concurrent,
                    max_concurrent=max_concurrent,
                )

            self._store[lease.id] = lease
            return LeaseAdmissionResult(
                status=LeaseAdmissionStatus.ADMITTED,
                available_quantity=available_quantity - lease.quantity,
                concurrent=concurrent + 1,
                max_concurrent=max_concurrent,
            )

    def compare_and_swap(self, lease: Lease, *, expected_fence: int) -> bool:
        with self._lock:
            current = self._store.get(lease.id)
            if (
                current is None
                or current.fence_token is None
                or current.fence_token.value != expected_fence
            ):
                return False
            self._store[lease.id] = lease
            return True

    def get(self, lease_id: str) -> Lease | None:
        with self._lock:
            return self._store.get(lease_id)

    def list(
        self,
        host_id: str | None = None,
        state: str | None = None,
    ) -> list[Lease]:
        with self._lock:
            result = list(self._store.values())
        if host_id:
            result = [item for item in result if item.budget.identity.host_id == host_id]
        if state:
            result = [item for item in result if item.state.value == state]
        return result

    def delete(self, lease_id: str) -> bool:
        with self._lock:
            if lease_id in self._store:
                del self._store[lease_id]
                return True
            return False
