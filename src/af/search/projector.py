"""Replay-safe search projection over transactional checkpoint evidence."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from af.framework.errors import PlatformError, PlatformReason
from af.framework.relational import DatabaseTransaction
from af.messaging.projections import (
    ProjectionDecision,
    TransactionalProjectionGuard,
)
from af.search.contracts import ResourceEnvelope
from af.search.lifecycle import ResourceIdentity

__all__ = (
    "SearchProjectionDelivery",
    "SearchProjectionError",
    "SearchProjectionKind",
    "SearchProjector",
)

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_MAX_POSITION = (1 << 63) - 1


class SearchProjectionError(PlatformError):
    """Sanitized search-projector failure."""


class SearchProjectionKind(StrEnum):
    """One idempotent mutation supported by the search projection boundary."""

    UPSERT = "upsert"
    DELETE = "delete"


class SearchProjectionSink(Protocol):
    """Narrow SearchManager-compatible mutation boundary."""

    def upsert(
        self,
        resources: tuple[ResourceEnvelope, ...],
        *,
        name: str | None = None,
    ) -> int: ...

    def delete(
        self,
        *,
        tenant_id: str,
        source_id: str,
        resource_id: str,
        name: str | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class SearchProjectionDelivery:
    """One contiguous source delivery and its exact idempotent search effect."""

    projection_name: str
    source_name: str
    message_id: str
    position: int
    processed_at: datetime
    kind: SearchProjectionKind
    resource: ResourceEnvelope | None = None
    identity: ResourceIdentity | None = None
    # Selects a SearchManager connection, not a logical index or alias.
    target_name: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "projection_name",
            _name(self.projection_name, "projection name"),
        )
        object.__setattr__(self, "source_name", _name(self.source_name, "source name"))
        object.__setattr__(self, "message_id", _message_id(self.message_id))
        if type(self.position) is not int or not 0 <= self.position <= _MAX_POSITION:
            raise ValueError("search projection position is invalid")
        if not isinstance(self.processed_at, datetime) or self.processed_at.utcoffset() is None:
            raise ValueError("search projection processed time must be timezone-aware")
        object.__setattr__(self, "processed_at", self.processed_at.astimezone(UTC))
        if not isinstance(self.kind, SearchProjectionKind):
            raise ValueError("search projection kind is invalid")
        if self.target_name is not None:
            object.__setattr__(
                self,
                "target_name",
                _name(self.target_name, "target name"),
            )
        if self.kind is SearchProjectionKind.UPSERT:
            valid = isinstance(self.resource, ResourceEnvelope) and self.identity is None
        else:
            valid = self.resource is None and isinstance(self.identity, ResourceIdentity)
        if not valid:
            raise ValueError("search projection must carry exactly one matching effect")

    def __repr__(self) -> str:
        return (
            "SearchProjectionDelivery("
            f"kind={self.kind.value!r}, position={self.position!r}, "
            "identity='[REDACTED]', message_id='[REDACTED]')"
        )


class SearchProjector:
    """Apply idempotent search effects with a durable contiguous checkpoint.

    The guard reserves inbox and checkpoint evidence in the caller-owned transaction.
    The transaction commits only after the search backend acknowledges the mutation.
    If acknowledgement or commit is lost, replay may repeat the stable upsert/tombstone;
    this boundary intentionally provides at-least-once execution, not exactly-once delivery.
    """

    def __init__(
        self,
        transaction_factory: Callable[[], AbstractContextManager[DatabaseTransaction]],
        search: SearchProjectionSink,
        *,
        guard: TransactionalProjectionGuard | None = None,
    ) -> None:
        if not callable(transaction_factory):
            raise TypeError("search projection transaction factory must be callable")
        if not callable(getattr(search, "upsert", None)) or not callable(
            getattr(search, "delete", None)
        ):
            raise TypeError("search projection sink is invalid")
        self._transaction_factory = transaction_factory
        self._search = search
        self._guard = TransactionalProjectionGuard() if guard is None else guard
        if not isinstance(self._guard, TransactionalProjectionGuard):
            raise TypeError("search projection guard must be transactional")

    def install(self) -> None:
        """Install durable inbox and checkpoint storage idempotently."""

        with self._transaction_factory() as transaction:
            self._guard.install(transaction)

    def project(self, delivery: SearchProjectionDelivery) -> ProjectionDecision:
        """Apply one delivery or classify it without repeating completed work."""

        if not isinstance(delivery, SearchProjectionDelivery):
            raise TypeError("search projection delivery is invalid")
        with self._transaction_factory() as transaction:
            decision = self._guard.prepare(
                transaction,
                projection_name=delivery.projection_name,
                source_name=delivery.source_name,
                message_id=delivery.message_id,
                position=delivery.position,
                processed_at=delivery.processed_at,
            )
            if decision is not ProjectionDecision.APPLY:
                return decision
            self._apply(delivery)
            return decision

    def _apply(self, delivery: SearchProjectionDelivery) -> None:
        try:
            if delivery.kind is SearchProjectionKind.UPSERT:
                resource = delivery.resource
                if resource is None:
                    raise SearchProjectionError(
                        PlatformReason.INTEGRITY_FAILURE,
                        stage="search.projector.effect",
                    )
                acknowledged = self._search.upsert(
                    (resource,),
                    name=delivery.target_name,
                )
                if type(acknowledged) is not int or acknowledged != 1:
                    raise SearchProjectionError(
                        PlatformReason.INTEGRITY_FAILURE,
                        stage="search.projector.acknowledgement",
                    )
                return

            identity = delivery.identity
            if identity is None:
                raise SearchProjectionError(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.projector.effect",
                )
            acknowledged_delete = self._search.delete(
                tenant_id=identity.tenant_id,
                source_id=identity.source_id,
                resource_id=identity.resource_id,
                name=delivery.target_name,
            )
            if type(acknowledged_delete) is not bool:
                raise SearchProjectionError(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.projector.acknowledgement",
                )
        except SearchProjectionError:
            raise
        except PlatformError:
            raise
        except Exception as error:
            raise SearchProjectionError(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projector.apply",
                protected_cause=error,
            ) from None


def _name(value: object, field_name: str) -> str:
    if type(value) is not str or _NAME.fullmatch(value) is None:
        raise ValueError(f"search projection {field_name} must be canonical")
    return value


def _message_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 200
        or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value)
    ):
        raise ValueError("search projection message id is invalid")
    return value
