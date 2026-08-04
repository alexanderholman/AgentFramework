"""In-memory reference driver for LeaseRepository."""

from __future__ import annotations

from typing import Optional

from af.domain.resources import Lease, LeaseRepository


class InMemoryLeaseRepository(LeaseRepository):
    """In-memory implementation of LeaseRepository for testing."""

    def __init__(self) -> None:
        self._store: dict[str, Lease] = {}

    def save(self, lease: Lease) -> None:
        self._store[lease.id] = lease

    def get(self, lease_id: str) -> Optional[Lease]:
        return self._store.get(lease_id)

    def list(
        self,
        host_id: Optional[str] = None,
        state: Optional[str] = None,
    ) -> list[Lease]:
        result = list(self._store.values())
        if host_id:
            result = [l for l in result if l.budget.identity.host_id == host_id]
        if state:
            result = [l for l in result if l.state.value == state]
        return result

    def delete(self, lease_id: str) -> bool:
        if lease_id in self._store:
            del self._store[lease_id]
            return True
        return False
