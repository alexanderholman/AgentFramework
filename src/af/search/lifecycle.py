"""Versioned search-index rebuild, cutover, rollback, and recovery coordination."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import RLock
from typing import Protocol

from af.framework.errors import PlatformError, PlatformReason
from af.search.contracts import ResourceEnvelope

__all__ = (
    "BulkIndexFailure",
    "BulkIndexResult",
    "IndexBuild",
    "IndexLifecycleBackend",
    "IndexLifecycleError",
    "IndexLifecycleManager",
    "IndexPhase",
    "IndexStatus",
    "ResourceIdentity",
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,254}[A-Za-z0-9])?$")
_MAX_BATCH = 10_000
_MAX_GENERATION = 99_999_999


class IndexLifecycleError(PlatformError):
    """Sanitized index-lifecycle failure."""


def _failure(
    reason: PlatformReason,
    *,
    stage: str,
    protected_cause: BaseException | None = None,
) -> IndexLifecycleError:
    return IndexLifecycleError(reason, stage=stage, protected_cause=protected_cause)


def _identifier(value: object, field_name: str, *, limit: int = 255) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > limit
        or _IDENTIFIER.fullmatch(value) is None
    ):
        raise ValueError(f"search index {field_name} must be canonical")
    return value


def _timestamp(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"search index {field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, order=True)
class ResourceIdentity:
    """Complete tenant-scoped resource identity for bulk outcomes."""

    tenant_id: str
    source_id: str
    resource_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _identifier(self.tenant_id, "tenant id"))
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source id"))
        object.__setattr__(self, "resource_id", _identifier(self.resource_id, "resource id"))

    @classmethod
    def from_resource(cls, resource: ResourceEnvelope) -> ResourceIdentity:
        """Create an identity from one validated canonical resource."""

        if not isinstance(resource, ResourceEnvelope):
            raise TypeError("resource identity requires a canonical resource")
        return cls(resource.tenant_id, resource.source_id, resource.resource_id)


@dataclass(frozen=True, slots=True)
class BulkIndexFailure:
    """One sanitized per-resource failure from a bulk write."""

    identity: ResourceIdentity
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ResourceIdentity):
            raise ValueError("bulk index failure identity is invalid")
        object.__setattr__(self, "reason", _identifier(self.reason, "failure reason"))


@dataclass(frozen=True, slots=True)
class BulkIndexResult:
    """Exhaustive, bounded outcome for one bulk index request."""

    attempted: tuple[ResourceIdentity, ...]
    succeeded: tuple[ResourceIdentity, ...] = ()
    failures: tuple[BulkIndexFailure, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.attempted) is not tuple
            or not self.attempted
            or len(self.attempted) > _MAX_BATCH
            or any(not isinstance(item, ResourceIdentity) for item in self.attempted)
            or len(set(self.attempted)) != len(self.attempted)
        ):
            raise ValueError("bulk index attempted identities are invalid")
        if (
            type(self.succeeded) is not tuple
            or any(not isinstance(item, ResourceIdentity) for item in self.succeeded)
            or len(set(self.succeeded)) != len(self.succeeded)
            or type(self.failures) is not tuple
            or any(not isinstance(item, BulkIndexFailure) for item in self.failures)
        ):
            raise ValueError("bulk index outcomes are invalid")
        attempted = set(self.attempted)
        succeeded = set(self.succeeded)
        failed = {item.identity for item in self.failures}
        if (
            len(failed) != len(self.failures)
            or succeeded & failed
            or succeeded | failed != attempted
        ):
            raise ValueError("bulk index outcomes must partition attempted identities")

    @property
    def complete(self) -> bool:
        """Whether every attempted identity succeeded."""

        return not self.failures


class IndexPhase(StrEnum):
    """Trusted lifecycle phases for one physical search index."""

    BUILDING = "building"
    READY = "ready"
    ACTIVE = "active"
    RETIRED = "retired"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class IndexBuild:
    """Immutable handle fencing operations to one generation."""

    logical_name: str
    physical_name: str
    generation: int
    projection_version: int
    base_index: str | None
    started_at: datetime

    def __post_init__(self) -> None:
        logical_name = _identifier(self.logical_name, "logical name", limit=200)
        object.__setattr__(self, "logical_name", logical_name)
        if type(self.generation) is not int or not 1 <= self.generation <= _MAX_GENERATION:
            raise ValueError("search index generation is invalid")
        expected = f"{logical_name}-g{self.generation:08d}"
        if self.physical_name != expected:
            raise ValueError("search physical index name does not match its generation")
        object.__setattr__(self, "physical_name", _identifier(expected, "physical name"))
        if type(self.projection_version) is not int or self.projection_version < 1:
            raise ValueError("search projection version must be positive")
        if self.base_index is not None:
            object.__setattr__(
                self,
                "base_index",
                _identifier(self.base_index, "base index"),
            )
        object.__setattr__(self, "started_at", _timestamp(self.started_at, "start time"))


@dataclass(frozen=True, slots=True)
class IndexStatus:
    """Immutable observable state for one rebuild generation."""

    build: IndexBuild
    phase: IndexPhase
    indexed_resources: int
    checkpoint: str | None
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.build, IndexBuild) or not isinstance(self.phase, IndexPhase):
            raise ValueError("search index status is invalid")
        if type(self.indexed_resources) is not int or self.indexed_resources < 0:
            raise ValueError("search indexed resource count is invalid")
        if self.checkpoint is not None:
            object.__setattr__(
                self,
                "checkpoint",
                _identifier(self.checkpoint, "checkpoint"),
            )
        object.__setattr__(self, "updated_at", _timestamp(self.updated_at, "update time"))


class IndexLifecycleBackend(Protocol):
    """Backend operations required for fenced lifecycle coordination."""

    def create_index(self, name: str, *, projection_version: int) -> None: ...

    def bulk_upsert(
        self,
        name: str,
        resources: tuple[ResourceEnvelope, ...],
    ) -> BulkIndexResult: ...

    def delete_index(self, name: str) -> None: ...

    def active_index(self, alias: str) -> str | None: ...

    def compare_and_swap_alias(
        self,
        alias: str,
        *,
        expected: str | None,
        replacement: str,
    ) -> bool: ...


class IndexLifecycleManager:
    """Serialize rebuilds and fence alias changes against observed state."""

    def __init__(
        self,
        backend: IndexLifecycleBackend,
        *,
        clock: Callable[[], datetime] | None = None,
        stale_after: timedelta = timedelta(hours=1),
    ) -> None:
        if not isinstance(stale_after, timedelta) or stale_after <= timedelta(0):
            raise ValueError("search stale-index interval must be positive")
        self._backend = backend
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._stale_after = stale_after
        self._lock = RLock()
        self._states: dict[str, IndexStatus] = {}
        self._generation: dict[str, int] = {}
        self._rollback: dict[str, tuple[str, str | None]] = {}

    def _now(self) -> datetime:
        return _timestamp(self._clock(), "clock")

    def _record(self, build: IndexBuild) -> IndexStatus:
        status = self._states.get(build.physical_name)
        if status is None or status.build != build:
            raise _failure(PlatformReason.INTEGRITY_FAILURE, stage="search.index.handle")
        return status

    def _replace(
        self,
        status: IndexStatus,
        *,
        phase: IndexPhase | None = None,
        indexed_resources: int | None = None,
        checkpoint: str | None = None,
    ) -> IndexStatus:
        updated = replace(
            status,
            phase=status.phase if phase is None else phase,
            indexed_resources=(
                status.indexed_resources if indexed_resources is None else indexed_resources
            ),
            checkpoint=status.checkpoint if checkpoint is None else checkpoint,
            updated_at=self._now(),
        )
        self._states[status.build.physical_name] = updated
        return updated

    def begin(self, logical_name: str, *, projection_version: int) -> IndexBuild:
        """Create a new isolated physical generation."""

        logical_name = _identifier(logical_name, "logical name", limit=200)
        if type(projection_version) is not int or projection_version < 1:
            raise ValueError("search projection version must be positive")
        with self._lock:
            try:
                base_index = self._backend.active_index(logical_name)
            except Exception as error:
                raise _failure(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="search.index.observe",
                    protected_cause=error,
                ) from None
            base_generation = 0
            if base_index is not None:
                base_index = _identifier(base_index, "base index")
                prefix = f"{logical_name}-g"
                if base_index.startswith(prefix) and base_index[len(prefix) :].isdigit():
                    base_generation = int(base_index[len(prefix) :])
            generation = max(self._generation.get(logical_name, 0), base_generation) + 1
            if generation > _MAX_GENERATION:
                raise _failure(
                    PlatformReason.CAPABILITY_UNAVAILABLE,
                    stage="search.index.generation",
                )
            started_at = self._now()
            build = IndexBuild(
                logical_name=logical_name,
                physical_name=f"{logical_name}-g{generation:08d}",
                generation=generation,
                projection_version=projection_version,
                base_index=base_index,
                started_at=started_at,
            )
            try:
                self._backend.create_index(
                    build.physical_name,
                    projection_version=projection_version,
                )
            except Exception as error:
                raise _failure(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="search.index.create",
                    protected_cause=error,
                ) from None
            self._generation[logical_name] = generation
            self._states[build.physical_name] = IndexStatus(
                build=build,
                phase=IndexPhase.BUILDING,
                indexed_resources=0,
                checkpoint=None,
                updated_at=started_at,
            )
            return build

    def apply(
        self,
        build: IndexBuild,
        resources: tuple[ResourceEnvelope, ...],
    ) -> BulkIndexResult:
        """Apply one bounded batch or fail the generation closed."""

        if (
            type(resources) is not tuple
            or not resources
            or len(resources) > _MAX_BATCH
            or any(not isinstance(item, ResourceEnvelope) for item in resources)
        ):
            raise ValueError("search rebuild resources must be a bounded non-empty tuple")
        if any(item.projection_version != build.projection_version for item in resources):
            raise ValueError("search resources do not match the rebuild projection version")
        requested = tuple(ResourceIdentity.from_resource(item) for item in resources)
        if len(set(requested)) != len(requested):
            raise ValueError("search rebuild resource identities must be unique")
        with self._lock:
            status = self._record(build)
            if status.phase is not IndexPhase.BUILDING:
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.index.bulk.phase",
                )
            try:
                result = self._backend.bulk_upsert(build.physical_name, resources)
            except Exception as error:
                self._replace(status, phase=IndexPhase.FAILED)
                raise _failure(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="search.index.bulk",
                    protected_cause=error,
                ) from None
            if not isinstance(result, BulkIndexResult) or result.attempted != requested:
                self._replace(status, phase=IndexPhase.FAILED)
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.index.bulk.result",
                )
            if not result.complete:
                self._replace(status, phase=IndexPhase.FAILED)
                return result
            self._replace(
                status,
                indexed_resources=status.indexed_resources + len(result.succeeded),
            )
            return result

    def mark_ready(self, build: IndexBuild, *, checkpoint: str) -> IndexStatus:
        """Seal a populated build at a validated source checkpoint."""

        checkpoint = _identifier(checkpoint, "checkpoint")
        with self._lock:
            status = self._record(build)
            if status.phase is not IndexPhase.BUILDING or status.indexed_resources == 0:
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.index.ready",
                )
            return self._replace(
                status,
                phase=IndexPhase.READY,
                checkpoint=checkpoint,
            )

    def cutover(self, build: IndexBuild) -> IndexStatus:
        """Atomically promote a ready generation if its base alias is unchanged."""

        with self._lock:
            status = self._record(build)
            if status.phase is not IndexPhase.READY:
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.index.cutover.phase",
                )
            try:
                current = self._backend.active_index(build.logical_name)
                changed = current == build.base_index and self._backend.compare_and_swap_alias(
                    build.logical_name,
                    expected=build.base_index,
                    replacement=build.physical_name,
                )
            except Exception as error:
                self._replace(status, phase=IndexPhase.FAILED)
                raise _failure(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="search.index.cutover",
                    protected_cause=error,
                ) from None
            if not changed:
                self._replace(status, phase=IndexPhase.FAILED)
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.index.cutover.fence",
                )
            base_index = build.base_index
            if base_index is not None and base_index in self._states:
                self._replace(self._states[base_index], phase=IndexPhase.RETIRED)
            self._rollback[build.logical_name] = (build.physical_name, build.base_index)
            return self._replace(status, phase=IndexPhase.ACTIVE)

    def rollback(self, logical_name: str) -> str:
        """Atomically restore the previous generation when it still owns history."""

        logical_name = _identifier(logical_name, "logical name", limit=200)
        with self._lock:
            target = self._rollback.get(logical_name)
            previous = None if target is None else target[1]
            if target is None or previous is None:
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.index.rollback.history",
                )
            current = target[0]
            try:
                changed = self._backend.compare_and_swap_alias(
                    logical_name,
                    expected=current,
                    replacement=previous,
                )
            except Exception as error:
                raise _failure(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="search.index.rollback",
                    protected_cause=error,
                ) from None
            if not changed:
                raise _failure(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.index.rollback.fence",
                )
            if current in self._states:
                self._replace(self._states[current], phase=IndexPhase.RETIRED)
            if previous in self._states:
                self._replace(self._states[previous], phase=IndexPhase.ACTIVE)
            del self._rollback[logical_name]
            return previous

    def recover_stale(self, logical_name: str) -> tuple[str, ...]:
        """Delete abandoned non-active generations after the configured interval."""

        logical_name = _identifier(logical_name, "logical name", limit=200)
        with self._lock:
            now = self._now()
            try:
                active = self._backend.active_index(logical_name)
            except Exception as error:
                raise _failure(
                    PlatformReason.PERMANENT_FAILURE,
                    stage="search.index.recovery.observe",
                    protected_cause=error,
                ) from None
            candidates = tuple(
                status
                for status in self._states.values()
                if status.build.logical_name == logical_name
                and status.build.physical_name != active
                and status.phase in {IndexPhase.BUILDING, IndexPhase.READY, IndexPhase.FAILED}
                and now - status.updated_at >= self._stale_after
            )
            recovered: list[str] = []
            for status in sorted(candidates, key=lambda item: item.build.generation):
                try:
                    self._backend.delete_index(status.build.physical_name)
                except Exception as error:
                    raise _failure(
                        PlatformReason.PERMANENT_FAILURE,
                        stage="search.index.recovery.delete",
                        protected_cause=error,
                    ) from None
                del self._states[status.build.physical_name]
                recovered.append(status.build.physical_name)
            return tuple(recovered)

    def status(self, build: IndexBuild) -> IndexStatus:
        """Return the immutable current state for one exact build handle."""

        with self._lock:
            return self._record(build)
