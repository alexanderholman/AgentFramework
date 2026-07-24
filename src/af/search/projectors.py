"""Replay-safe search projection over a durable caller-owned checkpoint transaction.

Search index writes and relational checkpoints are not one distributed transaction. Each target
must atomically commit a mutation with target-local delivery evidence before the relational
checkpoint can commit. A crash or commit failure can therefore leave the target one delivery
ahead, and retry reconciles that receipt without reapplying altered content. Target state behind
a committed relational checkpoint fails closed for rebuild instead of silently skipping missing
content. Each resource identity must also have one deterministic, immutable source owner within
a target generation. This contract provides replay-safe at-least-once convergence and does not
claim exactly-once delivery.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Never, Protocol

from af.framework.errors import PlatformError, PlatformReason
from af.framework.relational import DatabaseTransaction
from af.messaging.projections import (
    ProjectionCheckpoint,
    ProjectionCheckpointStore,
    ProjectionConflictError,
    ProjectionDecision,
    ProjectionGapError,
    ProjectionIntegrityError,
    TransactionalProjectionGuard,
)
from af.search.contracts import ResourceEnvelope
from af.search.lifecycle import IndexBuild, ResourceIdentity

__all__ = (
    "LifecycleSearchProjectionTarget",
    "PhysicalSearchProjectionTarget",
    "SearchProjectionDelivery",
    "SearchProjectionError",
    "SearchProjectionSourceOwner",
    "SearchProjectionTarget",
    "SearchProjectionTargetCheckpoint",
    "SearchTombstone",
    "TransactionalSearchProjector",
)

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_GENERATION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,254}[A-Za-z0-9])?$")
_UNSAFE_TEXT = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_MAX_POSITION = (1 << 63) - 1


class SearchProjectionError(PlatformError):
    """Sanitized failure while applying one search projection delivery."""


class PhysicalSearchProjectionTarget(Protocol):
    """Atomic projection mutations plus durable metadata for one physical target."""

    @property
    def physical_name(self) -> str: ...

    @property
    def generation(self) -> str: ...

    @property
    def projection_version(self) -> int: ...

    def projection_checkpoint(
        self,
        *,
        projection_name: str,
        source_name: str,
    ) -> SearchProjectionTargetCheckpoint | None:
        """Return the target-local head atomically associated with search content."""
        ...

    def apply_projection(
        self,
        delivery: SearchProjectionDelivery,
        /,
        *,
        projection_name: str,
        expected: SearchProjectionTargetCheckpoint | None,
    ) -> bool:
        """Commit mutation plus receipt, or return false for this exact committed receipt."""
        ...


class SearchProjectionTarget(PhysicalSearchProjectionTarget, Protocol):
    """Durably identified physical target accepted by the projector."""


class SearchProjectionSourceOwner(Protocol):
    """Resolve the one ordered source partition that owns a resource identity."""

    def __call__(self, identity: ResourceIdentity, /) -> str: ...


def _failure(
    reason: PlatformReason,
    *,
    stage: str,
    protected_cause: BaseException | None = None,
) -> SearchProjectionError:
    return SearchProjectionError(
        reason,
        stage=stage,
        protected_cause=protected_cause,
    )


def _name(value: object, field_name: str) -> str:
    if type(value) is not str or _NAME.fullmatch(value) is None:
        raise ValueError(f"search projection {field_name} must be canonical")
    return value


def _message_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 200
        or _UNSAFE_TEXT.search(value) is not None
    ):
        raise ValueError("search projection message id is invalid")
    return value


def _position(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_POSITION:
        raise ValueError("search projection position is invalid")
    return value


def _projection_version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_POSITION:
        raise ValueError("search projection version is invalid")
    return value


def _generation(value: object) -> str:
    if type(value) is not str or _GENERATION.fullmatch(value) is None:
        raise ValueError("search projection target generation is invalid")
    return value


def _aware_utc(value: object) -> datetime:
    if type(value) is not datetime or value.utcoffset() is None:
        raise ValueError("search projection clock must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True, repr=False)
class SearchProjectionTargetCheckpoint:
    """Target-local evidence atomically committed with one search mutation."""

    message_id: str
    position: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", _message_id(self.message_id))
        object.__setattr__(self, "position", _position(self.position))

    def __repr__(self) -> str:
        return (
            f"SearchProjectionTargetCheckpoint(message_id='[REDACTED]', position={self.position!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class LifecycleSearchProjectionTarget:
    """Bind a physical search driver to one immutable lifecycle build handle."""

    build: IndexBuild
    target: PhysicalSearchProjectionTarget

    def __post_init__(self) -> None:
        if not isinstance(self.build, IndexBuild):
            raise ValueError("search projection lifecycle build is invalid")
        try:
            checkpoint = self.target.projection_checkpoint
            apply = self.target.apply_projection
        except PlatformError:
            raise
        except Exception:
            raise TypeError("search projection physical target is invalid") from None
        if not callable(checkpoint) or not callable(apply):
            raise TypeError("search projection physical target is invalid")
        self._verify_fence()

    @property
    def physical_name(self) -> str:
        physical_name, _, _ = self._verify_fence()
        return physical_name

    @property
    def generation(self) -> str:
        _, generation, _ = self._verify_fence()
        return generation

    @property
    def projection_version(self) -> int:
        _, _, projection_version = self._verify_fence()
        return projection_version

    def projection_checkpoint(
        self,
        *,
        projection_name: str,
        source_name: str,
    ) -> SearchProjectionTargetCheckpoint | None:
        self._verify_fence()
        return self.target.projection_checkpoint(
            projection_name=projection_name,
            source_name=source_name,
        )

    def apply_projection(
        self,
        delivery: SearchProjectionDelivery,
        /,
        *,
        projection_name: str,
        expected: SearchProjectionTargetCheckpoint | None,
    ) -> bool:
        self._verify_fence()
        return self.target.apply_projection(
            delivery,
            projection_name=projection_name,
            expected=expected,
        )

    def _verify_fence(self) -> tuple[str, str, int]:
        try:
            physical_name = self.target.physical_name
            generation = self.target.generation
            projection_version = self.target.projection_version
        except PlatformError:
            raise
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projection.lifecycle-target",
                protected_cause=error,
            ) from None
        try:
            canonical_name = _generation(physical_name)
            canonical_generation = _generation(generation)
            canonical_version = _projection_version(projection_version)
        except ValueError as error:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.lifecycle-target",
                protected_cause=error,
            ) from None
        if (
            canonical_name != self.build.physical_name
            or canonical_version != self.build.projection_version
        ):
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.lifecycle-target",
            )
        return canonical_name, canonical_generation, canonical_version

    def __repr__(self) -> str:
        return "LifecycleSearchProjectionTarget(build='[REDACTED]', target='[REDACTED]')"


@dataclass(frozen=True, slots=True, repr=False)
class SearchTombstone:
    """One versioned tenant-scoped deletion from an ordered source."""

    identity: ResourceIdentity
    projection_version: int

    def __post_init__(self) -> None:
        if type(self.identity) is not ResourceIdentity:
            raise ValueError("search tombstone identity is invalid")
        object.__setattr__(
            self,
            "projection_version",
            _projection_version(self.projection_version),
        )

    def __repr__(self) -> str:
        return (
            "SearchTombstone(identity='[REDACTED]', "
            f"projection_version={self.projection_version!r})"
        )


type SearchMutation = ResourceEnvelope | SearchTombstone


@dataclass(frozen=True, slots=True, repr=False)
class SearchProjectionDelivery:
    """One immutable ordered upsert or tombstone delivery."""

    source_name: str
    message_id: str
    position: int
    mutation: SearchMutation

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_name", _name(self.source_name, "source name"))
        object.__setattr__(self, "message_id", _message_id(self.message_id))
        object.__setattr__(self, "position", _position(self.position))
        if type(self.mutation) not in (ResourceEnvelope, SearchTombstone):
            raise ValueError("search projection mutation is invalid")

    def __repr__(self) -> str:
        kind = "upsert" if isinstance(self.mutation, ResourceEnvelope) else "tombstone"
        return (
            "SearchProjectionDelivery("
            "source_name='[REDACTED]', message_id='[REDACTED]', "
            f"position={self.position!r}, mutation={kind!r})"
        )


def _control_flow_rank(error: BaseException) -> int:
    if not isinstance(error, Exception):
        return 3
    if isinstance(error, PlatformError):
        if error.reason is PlatformReason.CANCELLED:
            return 2
        if error.reason is PlatformReason.SHUTDOWN_FAILED:
            return 1
    return 0


def _sanitized_failure(error: BaseException, *, stage: str) -> BaseException:
    if not isinstance(error, Exception) or isinstance(error, PlatformError):
        return error
    return _failure(
        PlatformReason.PERMANENT_FAILURE,
        stage=stage,
        protected_cause=error,
    )


def _raise_preferred(errors: tuple[BaseException, ...]) -> Never:
    selected_index, selected = max(
        enumerate(errors),
        key=lambda item: (_control_flow_rank(item[1]), item[0]),
    )
    cause = (
        None
        if len(errors) == 1
        else errors[selected_index - 1]
        if selected_index > 0
        else errors[-1]
    )
    raise selected.with_traceback(selected.__traceback__) from cause


def _poison_after_cleanup_failure(
    rollback_only: Callable[[], object],
    errors: tuple[BaseException, ...],
) -> Never:
    try:
        rollback_only()
    except BaseException as rollback_failure:
        _raise_preferred(
            (
                *errors,
                _sanitized_failure(
                    rollback_failure,
                    stage="search.projection.transaction.rollback-only",
                ),
            )
        )
    _raise_preferred(errors)


@contextmanager
def _savepoint(
    transaction: DatabaseTransaction,
    name: str,
) -> Iterator[None]:
    """Keep checkpoint writes rollback-safe even when a caller catches an apply failure."""

    try:
        rollback_only = getattr(transaction, "mark_rollback_only", None)
    except BaseException as access:
        protected = _sanitized_failure(
            access,
            stage="search.projection.transaction.rollback-only",
        )
        raise protected.with_traceback(protected.__traceback__) from None
    if not callable(rollback_only):
        raise TypeError("search projection transaction cannot be marked rollback-only")
    try:
        context: AbstractContextManager[None] = transaction.savepoint(name)
    except BaseException as creation:
        _poison_after_cleanup_failure(
            rollback_only,
            (
                _sanitized_failure(
                    creation,
                    stage="search.projection.savepoint.create",
                ),
            ),
        )
    try:
        context.__enter__()
    except BaseException as entry:
        _poison_after_cleanup_failure(
            rollback_only,
            (
                _sanitized_failure(
                    entry,
                    stage="search.projection.savepoint.enter",
                ),
            ),
        )
    try:
        yield
    except BaseException as primary:
        protected_primary = _sanitized_failure(
            primary,
            stage="search.projection.transaction",
        )
        try:
            suppressed = context.__exit__(
                type(protected_primary),
                protected_primary,
                protected_primary.__traceback__,
            )
        except BaseException as cleanup:
            _poison_after_cleanup_failure(
                rollback_only,
                (
                    protected_primary,
                    _sanitized_failure(
                        cleanup,
                        stage="search.projection.savepoint.cleanup",
                    ),
                ),
            )
        if suppressed is not False and suppressed is not None:
            _poison_after_cleanup_failure(
                rollback_only,
                (
                    protected_primary,
                    _failure(
                        PlatformReason.INTEGRITY_FAILURE,
                        stage="search.projection.savepoint.cleanup",
                    ),
                ),
            )
        raise protected_primary.with_traceback(protected_primary.__traceback__) from None
    else:
        try:
            context.__exit__(None, None, None)
        except BaseException as cleanup:
            _poison_after_cleanup_failure(
                rollback_only,
                (
                    _sanitized_failure(
                        cleanup,
                        stage="search.projection.savepoint.cleanup",
                    ),
                ),
            )


class TransactionalSearchProjector:
    """Apply one ordered search mutation before committing its durable checkpoint.

    The caller owns the surrounding relational transaction and must commit successful
    decisions. Search mutations are deliberately performed before that commit. A failed or
    interrupted apply rolls the guard writes back to an internal savepoint even if the caller
    catches the error and commits other work. If savepoint cleanup itself fails, the transaction
    is marked rollback-only.

    ``initial_position`` is the first valid delivery for an empty checkpoint. A later position is
    valid only when the target generation already contains a complete snapshot through the
    preceding position; this class does not create or certify that snapshot.
    """

    def __init__(
        self,
        *,
        projection_name: str,
        initial_position: int,
        source_owner: SearchProjectionSourceOwner,
        target: SearchProjectionTarget,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._projection_name = _name(projection_name, "name")
        self._initial_position = _position(initial_position)
        if not callable(source_owner):
            raise TypeError("search projection source owner must be callable")
        try:
            target_physical_name = target.physical_name
            target_generation = target.generation
            target_projection_version = target.projection_version
            checkpoint = target.projection_checkpoint
            apply = target.apply_projection
        except PlatformError:
            raise
        except Exception:
            raise TypeError("search projection target is invalid") from None
        self._target_physical_name = _generation(target_physical_name)
        self._target_generation = _generation(target_generation)
        self._projection_version = _projection_version(target_projection_version)
        if not callable(checkpoint) or not callable(apply):
            raise TypeError("search projection target is invalid")
        if clock is not None and not callable(clock):
            raise TypeError("search projection clock must be callable")
        scope = (
            f"{self._projection_name}\0{self._target_physical_name}"
            f"\0{self._target_generation}\0{self._projection_version}"
        )
        digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        self._checkpoint_name = f"search.v{self._projection_version}.{digest}"
        self._target = target
        self._source_owner = source_owner
        self._guard = TransactionalProjectionGuard()
        self._checkpoints = ProjectionCheckpointStore()
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._savepoint_name = f"af_search_projection_{digest[:16]}"

    @property
    def checkpoint_name(self) -> str:
        """Durable target-generation and schema-version-bound checkpoint identity."""

        return self._checkpoint_name

    def install(self, transaction: DatabaseTransaction, /) -> None:
        """Install the existing durable inbox and checkpoint schema idempotently."""

        self._guard.install(transaction)

    def apply(
        self,
        transaction: DatabaseTransaction,
        delivery: SearchProjectionDelivery,
        /,
    ) -> ProjectionDecision:
        """Apply or skip one delivery under its durable source-order fence."""

        if type(delivery) is not SearchProjectionDelivery:
            raise TypeError("search projector requires a SearchProjectionDelivery")
        self._verify_target_fence()
        mutation = delivery.mutation
        if type(mutation) not in (ResourceEnvelope, SearchTombstone):
            raise ValueError("search projection mutation is invalid")
        if mutation.projection_version != self._projection_version:
            raise ValueError("search mutation does not match the projector version")
        self._verify_source_owner(delivery.source_name, mutation)
        processed_at = self._now()
        with _savepoint(transaction, self._savepoint_name):
            previous = self._checkpoints.load(
                transaction,
                projection_name=self._checkpoint_name,
                source_name=delivery.source_name,
            )
            if previous is None and delivery.position != self._initial_position:
                raise ProjectionGapError()
            decision = self._guard.prepare(
                transaction,
                projection_name=self._checkpoint_name,
                source_name=delivery.source_name,
                message_id=delivery.message_id,
                position=delivery.position,
                processed_at=processed_at,
                record_stale=False,
            )
            if decision is not ProjectionDecision.APPLY:
                committed = self._checkpoints.load(
                    transaction,
                    projection_name=self._checkpoint_name,
                    source_name=delivery.source_name,
                )
                if (
                    committed is None
                    or committed.position < delivery.position
                    or (
                        committed.position == delivery.position
                        and committed.message_id != delivery.message_id
                    )
                ):
                    raise ProjectionIntegrityError()
                self._verify_target_checkpoint(committed)
                return decision
            self._claim_target_advance(transaction, delivery.source_name)
            self._apply_target(delivery, previous)
            return decision

    def _verify_target_fence(self) -> None:
        try:
            physical_name = self._target.physical_name
            generation = self._target.generation
            projection_version = self._target.projection_version
        except PlatformError:
            raise
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projection.target-generation",
                protected_cause=error,
            ) from None
        try:
            current_name = _generation(physical_name)
            current = _generation(generation)
        except ValueError as error:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.target-generation",
                protected_cause=error,
            ) from None
        if current_name != self._target_physical_name or current != self._target_generation:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.target-generation",
            )
        try:
            current_version = _projection_version(projection_version)
        except ValueError as error:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.target-version",
                protected_cause=error,
            ) from None
        if current_version != self._projection_version:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.target-version",
            )

    def _verify_source_owner(
        self,
        source_name: str,
        mutation: SearchMutation,
    ) -> None:
        identity = (
            ResourceIdentity.from_resource(mutation)
            if isinstance(mutation, ResourceEnvelope)
            else mutation.identity
        )
        try:
            owner = self._source_owner(identity)
        except PlatformError:
            raise
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projection.source-ownership",
                protected_cause=error,
            ) from None
        try:
            canonical_owner = _name(owner, "source owner")
        except ValueError as error:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.source-ownership",
                protected_cause=error,
            ) from None
        if canonical_owner != source_name:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.source-ownership",
            )

    def _now(self) -> datetime:
        try:
            value = self._clock()
        except PlatformError:
            raise
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projection.clock",
                protected_cause=error,
            ) from None
        try:
            return _aware_utc(value)
        except PlatformError:
            raise
        except ValueError as error:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.clock",
                protected_cause=error,
            ) from None
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projection.clock",
                protected_cause=error,
            ) from None

    @staticmethod
    def _target_checkpoint(
        checkpoint: ProjectionCheckpoint | None,
    ) -> SearchProjectionTargetCheckpoint | None:
        if checkpoint is None:
            return None
        return SearchProjectionTargetCheckpoint(
            message_id=checkpoint.message_id,
            position=checkpoint.position,
        )

    def _claim_target_advance(
        self,
        transaction: DatabaseTransaction,
        source_name: str,
    ) -> None:
        claim_digest = hashlib.sha256(
            f"{self._checkpoint_name}\0{source_name}".encode()
        ).hexdigest()[:32]
        claim_name = f"af_search_projection_{claim_digest}"
        try:
            claim = transaction.claim_operation
        except PlatformError:
            raise
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projection.transaction.claim",
                protected_cause=error,
            ) from None
        if not callable(claim):
            raise _failure(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.projection.transaction.claim",
            )
        try:
            claimed = claim(claim_name)
        except PlatformError:
            raise
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projection.transaction.claim",
                protected_cause=error,
            ) from None
        if type(claimed) is not bool:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.transaction.claim",
            )
        if not claimed:
            raise _failure(
                PlatformReason.VALIDATION_FAILED,
                stage="search.projection.transaction.multiple-apply",
            )

    def _read_target_checkpoint(
        self,
        source_name: str,
    ) -> SearchProjectionTargetCheckpoint | None:
        self._verify_target_fence()
        try:
            checkpoint = self._target.projection_checkpoint(
                projection_name=self._checkpoint_name,
                source_name=source_name,
            )
        except PlatformError:
            raise
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage="search.projection.target-checkpoint",
                protected_cause=error,
            ) from None
        if checkpoint is not None and type(checkpoint) is not SearchProjectionTargetCheckpoint:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.projection.target-checkpoint",
            )
        return checkpoint

    def _verify_target_checkpoint(self, committed: ProjectionCheckpoint) -> None:
        observed = self._read_target_checkpoint(committed.source_name)
        expected = self._target_checkpoint(committed)
        if observed == expected:
            return
        if observed is not None and observed.position > committed.position:
            raise ProjectionConflictError()
        raise _failure(
            PlatformReason.INTEGRITY_FAILURE,
            stage="search.projection.target-checkpoint",
        )

    def _apply_target(
        self,
        delivery: SearchProjectionDelivery,
        expected: ProjectionCheckpoint | None,
    ) -> None:
        self._verify_target_fence()
        stage = (
            "search.projection.upsert"
            if isinstance(delivery.mutation, ResourceEnvelope)
            else "search.projection.delete"
        )
        try:
            acknowledged = self._target.apply_projection(
                delivery,
                projection_name=self._checkpoint_name,
                expected=self._target_checkpoint(expected),
            )
        except PlatformError:
            raise
        except Exception as error:
            raise _failure(
                PlatformReason.PERMANENT_FAILURE,
                stage=stage,
                protected_cause=error,
            ) from None
        if type(acknowledged) is not bool:
            raise _failure(
                PlatformReason.INTEGRITY_FAILURE,
                stage=f"{stage}.acknowledgement",
            )
        observed = self._read_target_checkpoint(delivery.source_name)
        expected_receipt = SearchProjectionTargetCheckpoint(
            message_id=delivery.message_id,
            position=delivery.position,
        )
        if observed == expected_receipt:
            return
        if observed is not None and observed.position > delivery.position:
            raise ProjectionConflictError()
        raise _failure(
            PlatformReason.INTEGRITY_FAILURE,
            stage="search.projection.target-acknowledgement",
        )
