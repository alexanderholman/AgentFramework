"""Host-local resource lease domain model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Protocol


class LeaseState(str, Enum):
    """Lease lifecycle states matching the issue specification."""

    RESERVED = "reserved"
    ACTIVE = "active"
    EXPIRED = "expired"
    RELEASED = "released"
    REVOKED = "revoked"
    RECONCILED = "reconciled"


class ResourceKind(str, Enum):
    """Resource types that can be leased."""

    CPU = "cpu"
    RAM = "ram"
    ACCELERATOR = "accelerator"
    STORAGE = "storage"
    IO = "io"
    CONCURRENCY = "concurrency"


@dataclass(frozen=True, slots=True)
class ResourceIdentity:
    """Immutable identity for a resource instance."""

    kind: ResourceKind
    host_id: str
    instance_id: str
    topology: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Immutable budget constraints for a resource."""

    identity: ResourceIdentity
    max_quantity: float
    max_duration: timedelta | None = None
    max_concurrent: int | None = None


@dataclass(frozen=True, slots=True)
class FenceToken:
    """Monotonically increasing token for fenced operations."""

    value: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Lease:
    """Immutable lease record with full lifecycle tracking."""

    id: str
    budget: ResourceBudget
    state: LeaseState
    quantity: float
    requested_at: datetime
    created_at: datetime
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    released_at: datetime | None = None
    fence_token: FenceToken | None = None
    invocation_id: str | None = None
    capability_digest: str | None = None
    runtime_digest: str | None = None
    authorized_by: str | None = None
    authorization_digest: str | None = None
    renewed_count: int = 0
    last_renewed_at: datetime | None = None
    scope: dict[str, str] | None = None


class LeaseRepository(Protocol):
    """Repository port for lease persistence."""

    def save(self, lease: Lease) -> None: ...

    def get(self, lease_id: str) -> Optional[Lease]: ...

    def list(
        self,
        host_id: Optional[str] = None,
        state: Optional[str] = None,
    ) -> list[Lease]: ...

    def delete(self, lease_id: str) -> bool: ...
