from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from shutil import copyfile
from typing import Literal, Protocol, cast

import pytest

from af.framework.errors import PlatformError, PlatformReason
from af.framework.relational import (
    DatabaseCursor,
    DatabaseOperationError,
    DatabaseRow,
    DatabaseStatement,
    DatabaseTransaction,
)
from af.framework.unit_of_work import UnitOfWork
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import (
    InboxRecord,
    ProjectionCheckpoint,
    ProjectionCheckpointStore,
    ProjectionDecision,
    ProjectionGapError,
    ProjectionIntegrityError,
    TransactionalInbox,
)
from af.search import projection_receipts as projector_module
from af.search.contracts import (
    ResourceEnvelope,
    ResourceKind,
    SearchFilter,
    SearchQuery,
)
from af.search.embedded import EmbeddedSearchDriver
from af.search.lifecycle import IndexBuild, ResourceIdentity
from af.search.projection_receipts import (
    LifecycleSearchProjectionTarget,
    SearchProjectionDelivery,
    SearchProjectionError,
    SearchProjectionTargetCheckpoint,
    SearchTombstone,
    TransactionalSearchProjector,
)

_NOW = datetime(2026, 7, 24, 18, tzinfo=UTC)
_SOURCE = "records.v1.partition-0"
_PROJECTION = "search.catalog"
_GENERATION = "catalog-g00000002"
_INSTANCE = "instance-00000000000000000000000000000001"
_DEFAULT_DELETE = object()


def _resource(
    resource_id: str = "resource-a",
    *,
    text: str = "original resilient energy projection",
    projection_version: int = 2,
    provenance: str = "a",
) -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=ResourceKind.TEXT,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{resource_id}",
        tenant_id="tenant-a",
        projection_version=projection_version,
        acl=("group:research",),
        provenance=("sha256:" + provenance * 64,),
        text=text,
    )


def _delivery(
    *,
    message_id: str,
    position: int,
    mutation: ResourceEnvelope | SearchTombstone | None = None,
) -> SearchProjectionDelivery:
    return SearchProjectionDelivery(
        source_name=_SOURCE,
        message_id=message_id,
        position=position,
        mutation=_resource() if mutation is None else mutation,
    )


class RecordingTarget:
    def __init__(
        self,
        physical_name: str = _GENERATION,
        generation: str = _INSTANCE,
        projection_version: int = 2,
    ) -> None:
        self.physical_name = physical_name
        self.generation = generation
        self.projection_version = projection_version
        self.resources: dict[ResourceIdentity, ResourceEnvelope] = {}
        self.calls: list[tuple[str, ResourceIdentity]] = []
        self.checkpoints: dict[
            tuple[str, str],
            SearchProjectionTargetCheckpoint,
        ] = {}
        self.upsert_ack: object = 1
        self.delete_ack: object = _DEFAULT_DELETE
        self.upsert_error: BaseException | None = None
        self.delete_error: BaseException | None = None

    def upsert(self, resources: tuple[ResourceEnvelope, ...], /) -> int:
        assert len(resources) == 1
        resource = resources[0]
        identity = ResourceIdentity.from_resource(resource)
        self.calls.append(("upsert", identity))
        if self.upsert_error is not None:
            raise self.upsert_error
        self.resources[identity] = resource
        return cast(int, self.upsert_ack)

    def delete(self, *, tenant_id: str, source_id: str, resource_id: str) -> bool:
        identity = ResourceIdentity(tenant_id, source_id, resource_id)
        self.calls.append(("delete", identity))
        if self.delete_error is not None:
            raise self.delete_error
        existed = self.resources.pop(identity, None) is not None
        if self.delete_ack is _DEFAULT_DELETE:
            return existed
        return cast(bool, self.delete_ack)

    def projection_checkpoint(
        self,
        *,
        projection_name: str,
        source_name: str,
    ) -> SearchProjectionTargetCheckpoint | None:
        return self.checkpoints.get((projection_name, source_name))

    def apply_projection(
        self,
        delivery: SearchProjectionDelivery,
        /,
        *,
        projection_name: str,
        expected: SearchProjectionTargetCheckpoint | None,
    ) -> bool:
        key = (projection_name, delivery.source_name)
        candidate = SearchProjectionTargetCheckpoint(
            message_id=delivery.message_id,
            position=delivery.position,
        )
        current = self.checkpoints.get(key)
        if current == candidate:
            return False
        if current != expected:
            raise SearchProjectionError(
                PlatformReason.INTEGRITY_FAILURE,
                stage="search.test.target-checkpoint",
            )
        if isinstance(delivery.mutation, ResourceEnvelope):
            acknowledged = self.upsert((delivery.mutation,))
            if type(acknowledged) is not int or acknowledged != 1:
                raise SearchProjectionError(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.projection.upsert.acknowledgement",
                )
        else:
            acknowledged = self.delete(
                tenant_id=delivery.mutation.identity.tenant_id,
                source_id=delivery.mutation.identity.source_id,
                resource_id=delivery.mutation.identity.resource_id,
            )
            if type(acknowledged) is not bool:
                raise SearchProjectionError(
                    PlatformReason.INTEGRITY_FAILURE,
                    stage="search.projection.delete.acknowledgement",
                )
        self.checkpoints[key] = candidate
        return True


class _ResourceSubclass(ResourceEnvelope):
    pass


class _DeliverySubclass(SearchProjectionDelivery):
    pass


def _setup(
    *,
    target: RecordingTarget | EmbeddedSearchDriver | None = None,
    database_path: str | Path = ":memory:",
    projection_version: int = 2,
) -> tuple[
    SQLiteDriver,
    RecordingTarget | EmbeddedSearchDriver,
    TransactionalSearchProjector,
]:
    database = SQLiteDriver(database_path)
    resolved_target = (
        RecordingTarget(projection_version=projection_version) if target is None else target
    )
    projector_target = (
        LifecycleSearchProjectionTarget(
            IndexBuild(
                logical_name="catalog",
                physical_name=_GENERATION,
                generation=2,
                projection_version=projection_version,
                base_index=None,
                started_at=_NOW,
            ),
            resolved_target,
        )
        if isinstance(resolved_target, EmbeddedSearchDriver)
        else resolved_target
    )
    projector = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=lambda _identity: _SOURCE,
        target=projector_target,
        clock=lambda: _NOW,
    )
    with database.transaction() as transaction:
        projector.install(transaction)
        projector.install(transaction)
    return database, resolved_target, projector


def _checkpoint(
    database: SQLiteDriver,
    projector: TransactionalSearchProjector,
) -> ProjectionCheckpoint | None:
    with database.transaction(read_only=True) as transaction:
        return ProjectionCheckpointStore().load(
            transaction,
            projection_name=projector.checkpoint_name,
            source_name=_SOURCE,
        )


def test_projector_commits_upsert_then_tombstone_with_contiguous_checkpoint() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    resource = _resource()
    identity = ResourceIdentity.from_resource(resource)

    with database.transaction() as transaction:
        decision = projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1, mutation=resource),
        )

    assert decision is ProjectionDecision.APPLY
    assert target.resources == {identity: resource}
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 1

    tombstone = SearchTombstone(identity=identity, projection_version=2)
    with database.transaction() as transaction:
        decision = projector.apply(
            transaction,
            _delivery(message_id="event-2", position=2, mutation=tombstone),
        )

    assert decision is ProjectionDecision.APPLY
    assert target.resources == {}
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 2
    assert checkpoint.message_id == "event-2"
    database.close()


def test_projector_composes_with_unit_of_work_transaction_contract() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)

    with UnitOfWork(database) as unit:
        assert (
            projector.apply(
                unit.transaction,
                _delivery(message_id="event-1", position=1),
            )
            is ProjectionDecision.APPLY
        )

    assert unit.committed is True
    assert target.calls
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 1
    database.close()


def test_duplicate_stale_and_gap_deliveries_never_touch_search() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    first = _delivery(message_id="event-1", position=1)

    with database.transaction() as transaction:
        assert projector.apply(transaction, first) is ProjectionDecision.APPLY
    target.calls.clear()

    with database.transaction() as transaction:
        assert projector.apply(transaction, first) is ProjectionDecision.DUPLICATE
    with database.transaction() as transaction:
        assert (
            projector.apply(
                transaction,
                _delivery(message_id="event-0", position=0),
            )
            is ProjectionDecision.STALE
        )
    for index in range(25):
        with database.transaction() as transaction:
            assert (
                projector.apply(
                    transaction,
                    _delivery(message_id=f"stale-{index}", position=0),
                )
                is ProjectionDecision.STALE
            )
    with (
        pytest.raises(ProjectionGapError),
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-3", position=3),
        )

    assert target.calls == []
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 1
    with database.transaction(read_only=True) as transaction:
        inbox_count = transaction.query(
            DatabaseStatement(
                """SELECT COUNT(*) AS count
                FROM af_inbox
                WHERE consumer_name = :projection_name
                  AND source_name = :source_name""",
                {
                    "projection_name": projector.checkpoint_name,
                    "source_name": _SOURCE,
                },
                read_only=True,
            )
        )
        assert (
            TransactionalInbox().find(
                transaction,
                consumer_name=projector.checkpoint_name,
                source_name=_SOURCE,
                message_id="event-3",
            )
            is None
        )
    assert inbox_count[0]["count"] == 1
    database.close()


def test_private_driver_failure_is_sanitized_and_rolls_back_checkpoint_savepoint() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    target.upsert_error = RuntimeError("private index credential 7f2d")

    with (
        database.transaction() as transaction,
        pytest.raises(SearchProjectionError) as captured,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.projection.upsert"
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)
    assert _checkpoint(database, projector) is None
    with database.transaction(read_only=True) as transaction:
        assert (
            TransactionalInbox().find(
                transaction,
                consumer_name=projector.checkpoint_name,
                source_name=_SOURCE,
                message_id="event-1",
            )
            is None
        )
    database.close()


def test_crash_after_upsert_before_checkpoint_commit_replays_idempotently() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    delivery = _delivery(message_id="event-1", position=1)
    identity = ResourceIdentity.from_resource(cast(ResourceEnvelope, delivery.mutation))

    with (
        pytest.raises(RuntimeError, match="simulated process loss"),
        database.transaction() as transaction,
    ):
        assert projector.apply(transaction, delivery) is ProjectionDecision.APPLY
        raise RuntimeError("simulated process loss")

    assert target.resources == {identity: delivery.mutation}
    assert _checkpoint(database, projector) is None

    with database.transaction() as transaction:
        assert projector.apply(transaction, delivery) is ProjectionDecision.APPLY

    assert target.resources == {identity: delivery.mutation}
    assert target.calls == [("upsert", identity)]
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 1
    database.close()


def test_crash_after_tombstone_replays_absent_delete_and_commits_checkpoint() -> None:
    resource = _resource()
    identity = ResourceIdentity.from_resource(resource)
    target = RecordingTarget()
    target.resources[identity] = resource
    database, _, projector = _setup(target=target)
    delivery = _delivery(
        message_id="event-1",
        position=1,
        mutation=SearchTombstone(identity=identity, projection_version=2),
    )

    with (
        pytest.raises(RuntimeError, match="simulated process loss"),
        database.transaction() as transaction,
    ):
        assert projector.apply(transaction, delivery) is ProjectionDecision.APPLY
        raise RuntimeError("simulated process loss")

    assert target.resources == {}
    assert _checkpoint(database, projector) is None

    with database.transaction() as transaction:
        assert projector.apply(transaction, delivery) is ProjectionDecision.APPLY

    assert target.resources == {}
    assert target.calls == [("delete", identity)]
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 1
    database.close()


def test_outer_transaction_cannot_move_one_source_more_than_one_target_ahead() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    first = _delivery(
        message_id="event-1",
        position=1,
        mutation=_resource("resource-a"),
    )
    second = _delivery(
        message_id="event-2",
        position=2,
        mutation=_resource("resource-b"),
    )

    with (
        pytest.raises(RuntimeError, match="simulated process loss"),
        database.transaction() as transaction,
    ):
        assert projector.apply(transaction, first) is ProjectionDecision.APPLY
        with pytest.raises(SearchProjectionError) as captured:
            projector.apply(transaction, second)
        assert captured.value.reason is PlatformReason.VALIDATION_FAILED
        assert captured.value.stage == "search.projection.transaction.multiple-apply"
        raise RuntimeError("simulated process loss")

    assert _checkpoint(database, projector) is None
    assert len(target.calls) == 1
    with database.transaction() as transaction:
        assert projector.apply(transaction, first) is ProjectionDecision.APPLY
    with database.transaction() as transaction:
        assert projector.apply(transaction, second) is ProjectionDecision.APPLY

    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 2
    assert len(target.calls) == 2
    database.close()


def test_rejected_second_apply_rolls_back_only_its_guard_writes() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    first = _delivery(message_id="event-1", position=1)
    second = _delivery(
        message_id="event-2",
        position=2,
        mutation=_resource("resource-b"),
    )

    with database.transaction() as transaction:
        assert projector.apply(transaction, first) is ProjectionDecision.APPLY
        with pytest.raises(SearchProjectionError) as captured:
            projector.apply(transaction, second)

    assert captured.value.reason is PlatformReason.VALIDATION_FAILED
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 1
    assert len(target.calls) == 1
    with database.transaction(read_only=True) as transaction:
        assert (
            TransactionalInbox().find(
                transaction,
                consumer_name=projector.checkpoint_name,
                source_name=_SOURCE,
                message_id="event-2",
            )
            is None
        )
    database.close()


def test_failed_target_attempt_consumes_the_outer_transaction_claim() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    delivery = _delivery(message_id="event-1", position=1)
    target.upsert_error = RuntimeError("private uncertain target failure")

    with database.transaction() as transaction:
        with pytest.raises(SearchProjectionError):
            projector.apply(transaction, delivery)
        target.upsert_error = None
        with pytest.raises(SearchProjectionError) as captured:
            projector.apply(transaction, delivery)

    assert captured.value.reason is PlatformReason.VALIDATION_FAILED
    assert captured.value.stage == "search.projection.transaction.multiple-apply"
    assert _checkpoint(database, projector) is None
    with database.transaction() as transaction:
        assert projector.apply(transaction, delivery) is ProjectionDecision.APPLY
    assert _checkpoint(database, projector) is not None
    database.close()


@pytest.mark.parametrize(
    ("mutation", "attribute", "ack"),
    [
        (_resource(), "upsert_ack", 0),
        (_resource(), "upsert_ack", True),
        (
            SearchTombstone(
                identity=ResourceIdentity("tenant-a", "source-primary", "resource-a"),
                projection_version=2,
            ),
            "delete_ack",
            1,
        ),
    ],
)
def test_invalid_search_acknowledgement_fails_integrity_without_checkpoint(
    mutation: ResourceEnvelope | SearchTombstone,
    attribute: str,
    ack: object,
) -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    setattr(target, attribute, ack)

    with (
        database.transaction() as transaction,
        pytest.raises(SearchProjectionError) as captured,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1, mutation=mutation),
        )

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured.value.stage.endswith("acknowledgement")
    assert _checkpoint(database, projector) is None
    database.close()


def test_control_flow_failures_escape_unchanged_and_roll_back_checkpoint() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    interruption = KeyboardInterrupt()
    target.upsert_error = interruption

    with database.transaction() as transaction, pytest.raises(KeyboardInterrupt) as captured:
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value is interruption
    assert _checkpoint(database, projector) is None

    cancellation = PlatformError(
        PlatformReason.CANCELLED,
        stage="search.test.cancelled",
    )
    target.upsert_error = cancellation
    with (
        database.transaction() as transaction,
        pytest.raises(PlatformError) as captured_platform,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured_platform.value is cancellation
    assert _checkpoint(database, projector) is None
    database.close()


def test_projector_rejects_wrong_version_and_redacts_delivery_representations() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    resource = _resource(text="private source content", projection_version=3)
    delivery = _delivery(
        message_id="private-message-id",
        position=1,
        mutation=resource,
    )

    with pytest.raises(ValueError), database.transaction() as transaction:
        projector.apply(transaction, delivery)

    assert target.calls == []
    assert "private" not in repr(delivery)
    assert "private" not in repr(
        SearchTombstone(
            identity=ResourceIdentity("tenant-a", "source-primary", "private-resource"),
            projection_version=2,
        )
    )
    with pytest.raises(TypeError), database.transaction() as transaction:
        projector.apply(transaction, object())  # type: ignore[arg-type]
    database.close()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: SearchProjectionDelivery(
            source_name="INVALID SOURCE",
            message_id="event-1",
            position=1,
            mutation=_resource(),
        ),
        lambda: SearchProjectionDelivery(
            source_name=_SOURCE,
            message_id="",
            position=1,
            mutation=_resource(),
        ),
        lambda: SearchProjectionDelivery(
            source_name=_SOURCE,
            message_id="event-1",
            position=-1,
            mutation=_resource(),
        ),
        lambda: SearchProjectionDelivery(
            source_name=_SOURCE,
            message_id="event-1",
            position=1 << 63,
            mutation=_resource(),
        ),
        lambda: SearchProjectionDelivery(
            source_name=_SOURCE,
            message_id="event-1",
            position=1,
            mutation=object(),  # type: ignore[arg-type]
        ),
        lambda: SearchProjectionDelivery(
            source_name=_SOURCE,
            message_id="event-1",
            position=1,
            mutation=_ResourceSubclass(
                resource_id="resource-a",
                kind=ResourceKind.TEXT,
                source_id="source-primary",
                authoritative_locator="af://source-primary/resource-a",
                tenant_id="tenant-a",
                projection_version=2,
                acl=("group:research",),
                provenance=("sha256:" + "a" * 64,),
                text="subclass content",
            ),
        ),
        lambda: SearchTombstone(
            identity=object(),  # type: ignore[arg-type]
            projection_version=2,
        ),
        lambda: SearchTombstone(
            identity=ResourceIdentity("tenant-a", "source-primary", "resource-a"),
            projection_version=0,
        ),
    ],
)
def test_projection_delivery_contracts_reject_invalid_identity_and_bounds(
    factory: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        factory()


def test_projector_constructor_rejects_invalid_dependencies() -> None:
    target = RecordingTarget()
    with pytest.raises(ValueError):
        TransactionalSearchProjector(
            projection_name="INVALID NAME",
            initial_position=1,
            source_owner=lambda _identity: _SOURCE,
            target=target,
        )
    with pytest.raises(ValueError):
        TransactionalSearchProjector(
            projection_name=_PROJECTION,
            initial_position=1,
            source_owner=lambda _identity: _SOURCE,
            target=RecordingTarget(projection_version=0),
        )
    with pytest.raises(ValueError):
        TransactionalSearchProjector(
            projection_name=_PROJECTION,
            initial_position=1,
            source_owner=lambda _identity: _SOURCE,
            target=RecordingTarget("INVALID GENERATION"),
        )
    with pytest.raises(ValueError):
        TransactionalSearchProjector(
            projection_name=_PROJECTION,
            initial_position=-1,
            source_owner=lambda _identity: _SOURCE,
            target=target,
        )
    with pytest.raises(TypeError):
        TransactionalSearchProjector(
            projection_name=_PROJECTION,
            initial_position=1,
            source_owner=lambda _identity: _SOURCE,
            target=object(),  # type: ignore[arg-type]
        )

    class NonCallableTarget:
        generation = _GENERATION
        upsert = 1
        delete = 2

    with pytest.raises(TypeError):
        TransactionalSearchProjector(
            projection_name=_PROJECTION,
            initial_position=1,
            source_owner=lambda _identity: _SOURCE,
            target=NonCallableTarget(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        TransactionalSearchProjector(
            projection_name=_PROJECTION,
            initial_position=1,
            target=target,
            source_owner=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError):
        TransactionalSearchProjector(
            projection_name=_PROJECTION,
            initial_position=1,
            source_owner=lambda _identity: _SOURCE,
            target=target,
            clock=object(),  # type: ignore[arg-type]
        )


def test_projector_rejects_delivery_subclasses_before_field_access() -> None:
    database, _, projector = _setup()
    delivery = _DeliverySubclass(
        source_name=_SOURCE,
        message_id="event-1",
        position=1,
        mutation=_resource(),
    )

    with pytest.raises(TypeError), database.transaction() as transaction:
        projector.apply(transaction, delivery)

    assert _checkpoint(database, projector) is None
    database.close()


def test_checkpoint_identity_binds_logical_name_generation_and_version() -> None:
    database, _, first = _setup()
    first_delivery = _delivery(message_id="event-1", position=1)
    with database.transaction() as transaction:
        assert first.apply(transaction, first_delivery) is ProjectionDecision.APPLY

    second_target = RecordingTarget("catalog.g00000003")
    second = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=lambda _identity: _SOURCE,
        target=second_target,
        clock=lambda: _NOW,
    )
    with database.transaction() as transaction:
        assert second.apply(transaction, first_delivery) is ProjectionDecision.APPLY

    third_target = RecordingTarget(projection_version=3)
    third = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=lambda _identity: _SOURCE,
        target=third_target,
        clock=lambda: _NOW,
    )
    version_three = replace(
        cast(ResourceEnvelope, first_delivery.mutation),
        projection_version=3,
        text="version three projection",
    )
    with database.transaction() as transaction:
        assert (
            third.apply(
                transaction,
                replace(first_delivery, mutation=version_three),
            )
            is ProjectionDecision.APPLY
        )

    assert len({first.checkpoint_name, second.checkpoint_name, third.checkpoint_name}) == 3
    assert second_target.calls
    assert third_target.calls
    database.close()


def test_projector_fails_closed_if_target_generation_changes() -> None:
    target = RecordingTarget()
    database, _, projector = _setup(target=target)
    original_checkpoint_name = projector.checkpoint_name
    target.generation = "instance-00000000000000000000000000000002"

    with (
        pytest.raises(SearchProjectionError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured.value.stage == "search.projection.target-generation"
    assert projector.checkpoint_name == original_checkpoint_name
    assert target.calls == []
    assert _checkpoint(database, projector) is None
    database.close()


def test_projector_fails_closed_if_target_projection_version_changes() -> None:
    target = RecordingTarget()
    database, _, projector = _setup(target=target)
    target.projection_version = 3

    with (
        pytest.raises(SearchProjectionError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured.value.stage == "search.projection.target-version"
    assert target.calls == []
    assert _checkpoint(database, projector) is None
    database.close()


def test_lifecycle_target_rejects_a_different_physical_name_or_version() -> None:
    build = IndexBuild(
        logical_name="catalog",
        physical_name=_GENERATION,
        generation=2,
        projection_version=2,
        base_index=None,
        started_at=_NOW,
    )

    with pytest.raises(SearchProjectionError) as captured:
        LifecycleSearchProjectionTarget(
            build,
            RecordingTarget("catalog-g00000003"),
        )

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured.value.stage == "search.projection.lifecycle-target"
    assert "catalog" not in str(captured.value)

    with pytest.raises(SearchProjectionError) as version:
        LifecycleSearchProjectionTarget(
            build,
            RecordingTarget(projection_version=3),
        )
    assert version.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert version.value.stage == "search.projection.lifecycle-target"


def test_unseeded_projector_rejects_unexpected_first_position() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)

    with (
        pytest.raises(ProjectionGapError),
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-10", position=10),
        )

    assert target.calls == []
    assert _checkpoint(database, projector) is None
    database.close()


def test_orphaned_inbox_evidence_cannot_skip_an_unseeded_target() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    with database.transaction() as transaction:
        TransactionalInbox().record(
            transaction,
            InboxRecord(
                consumer_name=projector.checkpoint_name,
                source_name=_SOURCE,
                message_id="event-1",
                position=1,
                processed_at=_NOW,
            ),
        )

    with (
        pytest.raises(ProjectionIntegrityError),
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert target.calls == []
    assert _checkpoint(database, projector) is None
    database.close()


def test_inbox_evidence_ahead_of_checkpoint_cannot_skip_target_mutation() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    with database.transaction() as transaction:
        assert (
            projector.apply(
                transaction,
                _delivery(message_id="event-1", position=1),
            )
            is ProjectionDecision.APPLY
        )
    target.calls.clear()
    with database.transaction() as transaction:
        TransactionalInbox().record(
            transaction,
            InboxRecord(
                consumer_name=projector.checkpoint_name,
                source_name=_SOURCE,
                message_id="event-2",
                position=2,
                processed_at=_NOW,
            ),
        )

    with (
        pytest.raises(ProjectionIntegrityError),
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(
                message_id="event-2",
                position=2,
                mutation=_resource("resource-b"),
            ),
        )

    assert target.calls == []
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 1
    database.close()


def test_concurrent_first_commit_is_validated_and_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    delivery = _delivery(message_id="event-1", position=1)
    original_load = projector._checkpoints.load
    inserted = False

    def commit_before_guard(
        transaction: DatabaseTransaction,
        /,
        *,
        projection_name: str,
        source_name: str,
    ) -> ProjectionCheckpoint | None:
        nonlocal inserted
        observed = original_load(
            transaction,
            projection_name=projection_name,
            source_name=source_name,
        )
        if not inserted:
            assert observed is None
            inserted = True
            assert target.apply_projection(
                delivery,
                projection_name=projector.checkpoint_name,
                expected=None,
            )
            target.calls.clear()
            checkpoint = ProjectionCheckpoint(
                projection_name=projector.checkpoint_name,
                source_name=_SOURCE,
                position=1,
                message_id="event-1",
                updated_at=_NOW,
            )
            ProjectionCheckpointStore().start(transaction, checkpoint)
            TransactionalInbox().record(
                transaction,
                InboxRecord(
                    consumer_name=projector.checkpoint_name,
                    source_name=_SOURCE,
                    message_id="event-1",
                    position=1,
                    processed_at=_NOW,
                ),
            )
        return observed

    monkeypatch.setattr(projector._checkpoints, "load", commit_before_guard)
    with database.transaction() as transaction:
        decision = projector.apply(
            transaction,
            delivery,
        )

    assert decision is ProjectionDecision.DUPLICATE
    assert target.calls == []
    checkpoint = _checkpoint(database, projector)
    assert checkpoint is not None
    assert checkpoint.position == 1
    database.close()


def test_concurrent_uncommitted_first_delivery_is_retryable_then_duplicate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-projector.sqlite3"
    first = SQLiteDriver(path)
    second = SQLiteDriver(path)
    target = RecordingTarget()
    projector = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=lambda _identity: _SOURCE,
        target=target,
        clock=lambda: _NOW,
    )
    with first.transaction() as transaction:
        projector.install(transaction)

    delivery = _delivery(message_id="event-1", position=1)
    with first.transaction() as winner:
        assert projector.apply(winner, delivery) is ProjectionDecision.APPLY
        # Current SQLite acquires the writer fence at BEGIN IMMEDIATE, so a
        # competing delivery may be refused before its first statement.
        with pytest.raises(DatabaseOperationError) as captured, second.transaction() as loser:
            projector.apply(loser, delivery)

    assert captured.value.reason is PlatformReason.TRANSIENT_FAILURE
    assert target.calls == [
        ("upsert", ResourceIdentity.from_resource(cast(ResourceEnvelope, delivery.mutation)))
    ]
    with second.transaction() as retry:
        assert projector.apply(retry, delivery) is ProjectionDecision.DUPLICATE
    assert len(target.calls) == 1
    first.close()
    second.close()


def test_projector_rejects_mutation_from_another_ordered_source_partition() -> None:
    database = SQLiteDriver(":memory:")
    target = RecordingTarget()
    projector = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=lambda _identity: "records.v1.partition-1",
        target=target,
        clock=lambda: _NOW,
    )
    with database.transaction() as transaction:
        projector.install(transaction)

    with (
        pytest.raises(SearchProjectionError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured.value.stage == "search.projection.source-ownership"
    assert target.calls == []
    assert _checkpoint(database, projector) is None
    database.close()


@pytest.mark.parametrize(
    ("source_owner", "reason"),
    [
        (
            lambda _identity: (_ for _ in ()).throw(RuntimeError("private ownership map")),
            PlatformReason.PERMANENT_FAILURE,
        ),
        (lambda _identity: "INVALID OWNER", PlatformReason.INTEGRITY_FAILURE),
    ],
)
def test_source_owner_failures_are_sanitized_before_checkpoint_mutation(
    source_owner: Callable[[ResourceIdentity], str],
    reason: PlatformReason,
) -> None:
    database = SQLiteDriver(":memory:")
    target = RecordingTarget()
    projector = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=source_owner,
        target=target,
        clock=lambda: _NOW,
    )
    with database.transaction() as transaction:
        projector.install(transaction)

    with (
        pytest.raises(SearchProjectionError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value.reason is reason
    assert captured.value.stage == "search.projection.source-ownership"
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)
    assert target.calls == []
    assert _checkpoint(database, projector) is None
    database.close()


def test_source_owner_platform_cancellation_escapes_unchanged() -> None:
    cancellation = PlatformError(
        PlatformReason.CANCELLED,
        stage="search.test.source-owner-cancelled",
    )
    database = SQLiteDriver(":memory:")
    projector = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=lambda _identity: (_ for _ in ()).throw(cancellation),
        target=RecordingTarget(),
        clock=lambda: _NOW,
    )
    with database.transaction() as transaction:
        projector.install(transaction)

    with (
        pytest.raises(PlatformError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value is cancellation
    assert _checkpoint(database, projector) is None
    database.close()


class _HostileDatetime(datetime):
    def utcoffset(self) -> timedelta | None:
        raise RuntimeError("private clock credential")


class _HostileTimezone(tzinfo):
    def utcoffset(self, value: datetime | None) -> timedelta | None:
        del value
        raise RuntimeError("private timezone credential")

    def dst(self, value: datetime | None) -> timedelta | None:
        del value
        return timedelta(0)

    def tzname(self, value: datetime | None) -> str | None:
        del value
        return "hostile"


@pytest.mark.parametrize(
    ("clock", "reason"),
    [
        (
            lambda: (_ for _ in ()).throw(RuntimeError("private clock state")),
            PlatformReason.PERMANENT_FAILURE,
        ),
        (lambda: datetime(2026, 7, 24, 18), PlatformReason.INTEGRITY_FAILURE),
        (
            lambda: _HostileDatetime(2026, 7, 24, 18, tzinfo=UTC),
            PlatformReason.INTEGRITY_FAILURE,
        ),
        (
            lambda: datetime(2026, 7, 24, 18, tzinfo=_HostileTimezone()),
            PlatformReason.PERMANENT_FAILURE,
        ),
    ],
)
def test_clock_failures_are_sanitized_before_checkpoint_mutation(
    clock: Callable[[], datetime],
    reason: PlatformReason,
) -> None:
    database = SQLiteDriver(":memory:")
    target = RecordingTarget()
    projector = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=lambda _identity: _SOURCE,
        target=target,
        clock=clock,
    )
    with database.transaction() as transaction:
        projector.install(transaction)
    with (
        pytest.raises(SearchProjectionError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value.reason is reason
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)
    assert _checkpoint(database, projector) is None
    database.close()


def test_clock_platform_cancellation_escapes_unchanged() -> None:
    database = SQLiteDriver(":memory:")
    cancellation = PlatformError(
        PlatformReason.CANCELLED,
        stage="search.test.clock-cancelled",
    )
    projector = TransactionalSearchProjector(
        projection_name=_PROJECTION,
        initial_position=1,
        source_owner=lambda _identity: _SOURCE,
        target=RecordingTarget(),
        clock=lambda: (_ for _ in ()).throw(cancellation),
    )
    with database.transaction() as transaction:
        projector.install(transaction)

    with (
        pytest.raises(PlatformError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(message_id="event-1", position=1),
        )

    assert captured.value is cancellation
    assert _checkpoint(database, projector) is None
    database.close()


def test_private_delete_failure_is_sanitized_and_rolls_back_checkpoint() -> None:
    resource = _resource()
    identity = ResourceIdentity.from_resource(resource)
    target = RecordingTarget()
    target.delete_error = RuntimeError("private deletion token")
    database, _, projector = _setup(target=target)

    with (
        pytest.raises(SearchProjectionError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(
                message_id="event-1",
                position=1,
                mutation=SearchTombstone(identity, 2),
            ),
        )

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.projection.delete"
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)
    assert _checkpoint(database, projector) is None
    database.close()


def test_delete_platform_cancellation_escapes_unchanged() -> None:
    resource = _resource()
    target = RecordingTarget()
    cancellation = PlatformError(
        PlatformReason.CANCELLED,
        stage="search.test.delete-cancelled",
    )
    target.delete_error = cancellation
    database, _, projector = _setup(target=target)

    with (
        pytest.raises(PlatformError) as captured,
        database.transaction() as transaction,
    ):
        projector.apply(
            transaction,
            _delivery(
                message_id="event-1",
                position=1,
                mutation=SearchTombstone(
                    ResourceIdentity.from_resource(resource),
                    2,
                ),
            ),
        )

    assert captured.value is cancellation
    assert _checkpoint(database, projector) is None
    database.close()


class _FailingSavepoint:
    def __init__(self, cleanup: BaseException) -> None:
        self.cleanup = cleanup

    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> bool:
        del args
        raise self.cleanup


class _SavepointTransaction:
    def __init__(self, cleanup: BaseException) -> None:
        self.cleanup = cleanup
        self.rollback_only = False

    def savepoint(self, name: str, /) -> _FailingSavepoint:
        assert name == "projector_savepoint"
        return _FailingSavepoint(self.cleanup)

    def mark_rollback_only(self) -> None:
        self.rollback_only = True


class _NoRollbackOnlyTransaction:
    def savepoint(self, name: str, /) -> _FailingSavepoint:
        assert name == "projector_savepoint"
        return _FailingSavepoint(RuntimeError("unused"))


class _RollbackOnlyDescriptorFailure:
    @property
    def mark_rollback_only(self) -> Callable[[], object]:
        raise RuntimeError("private rollback-only descriptor credential")


@pytest.mark.parametrize(
    "primary",
    [
        KeyboardInterrupt(),
        PlatformError(
            PlatformReason.SHUTDOWN_FAILED,
            stage="search.test.shutdown",
        ),
    ],
)
def test_savepoint_cleanup_does_not_replace_higher_priority_control_flow(
    primary: BaseException,
) -> None:
    with (
        pytest.raises(type(primary)) as captured,
        projector_module._savepoint(
            cast(
                DatabaseTransaction,
                _SavepointTransaction(RuntimeError("private cleanup failure")),
            ),
            "projector_savepoint",
        ),
    ):
        raise primary

    assert captured.value is primary


def test_savepoint_cleanup_cancellation_replaces_ordinary_failure() -> None:
    cancellation = PlatformError(
        PlatformReason.CANCELLED,
        stage="search.test.cleanup-cancelled",
    )
    with (
        pytest.raises(PlatformError) as captured,
        projector_module._savepoint(
            cast(DatabaseTransaction, _SavepointTransaction(cancellation)),
            "projector_savepoint",
        ),
    ):
        raise RuntimeError("private primary failure")

    assert captured.value is cancellation


def test_savepoint_requires_an_outer_rollback_only_fence() -> None:
    with (
        pytest.raises(TypeError, match="rollback-only"),
        projector_module._savepoint(
            cast(DatabaseTransaction, _NoRollbackOnlyTransaction()),
            "projector_savepoint",
        ),
    ):
        pass


def test_rollback_only_descriptor_failure_is_sanitized() -> None:
    with (
        pytest.raises(SearchProjectionError) as captured,
        projector_module._savepoint(
            cast(DatabaseTransaction, _RollbackOnlyDescriptorFailure()),
            "projector_savepoint",
        ),
    ):
        pass

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.projection.transaction.rollback-only"
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)


def test_successful_body_cleanup_failure_marks_transaction_rollback_only() -> None:
    transaction = _SavepointTransaction(RuntimeError("private cleanup failure"))

    with (
        pytest.raises(SearchProjectionError) as captured,
        projector_module._savepoint(
            cast(DatabaseTransaction, transaction),
            "projector_savepoint",
        ),
    ):
        pass

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.projection.savepoint.cleanup"
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)
    assert transaction.rollback_only is True


class _CreationFailingTransaction:
    def __init__(self) -> None:
        self.rollback_only = False

    def savepoint(self, name: str, /) -> AbstractContextManager[None]:
        assert name == "projector_savepoint"
        raise RuntimeError("private savepoint factory credential")

    def mark_rollback_only(self) -> None:
        self.rollback_only = True


class _EntryFailingSavepoint:
    def __enter__(self) -> None:
        raise RuntimeError("private savepoint entry credential")

    def __exit__(self, *args: object) -> None:
        del args


class _EntryFailingTransaction:
    def __init__(self) -> None:
        self.rollback_only = False

    def savepoint(self, name: str, /) -> AbstractContextManager[None]:
        assert name == "projector_savepoint"
        return _EntryFailingSavepoint()

    def mark_rollback_only(self) -> None:
        self.rollback_only = True


@pytest.mark.parametrize(
    ("transaction", "stage"),
    [
        (
            _CreationFailingTransaction(),
            "search.projection.savepoint.create",
        ),
        (
            _EntryFailingTransaction(),
            "search.projection.savepoint.enter",
        ),
    ],
)
def test_savepoint_boundary_failures_are_sanitized_and_poison_transaction(
    transaction: _CreationFailingTransaction | _EntryFailingTransaction,
    stage: str,
) -> None:
    with (
        pytest.raises(SearchProjectionError) as captured,
        projector_module._savepoint(
            cast(DatabaseTransaction, transaction),
            "projector_savepoint",
        ),
    ):
        pass

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == stage
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)
    assert transaction.rollback_only is True


class _PoisonFailureTransaction(_SavepointTransaction):
    def __init__(self, cleanup: BaseException, poison: BaseException) -> None:
        super().__init__(cleanup)
        self.poison = poison

    def mark_rollback_only(self) -> None:
        raise self.poison


def test_rollback_only_cancellation_precedes_ordinary_cleanup_failures() -> None:
    cancellation = PlatformError(
        PlatformReason.CANCELLED,
        stage="search.test.poison-cancelled",
    )
    transaction = _PoisonFailureTransaction(
        RuntimeError("private cleanup failure"),
        cancellation,
    )

    with (
        pytest.raises(PlatformError) as captured,
        projector_module._savepoint(
            cast(DatabaseTransaction, transaction),
            "projector_savepoint",
        ),
    ):
        raise RuntimeError("private primary failure")

    assert captured.value is cancellation


def test_rollback_only_failure_is_sanitized_after_cleanup_failure() -> None:
    transaction = _PoisonFailureTransaction(
        RuntimeError("private cleanup failure"),
        RuntimeError("private rollback-only credential"),
    )

    with (
        pytest.raises(SearchProjectionError) as captured,
        projector_module._savepoint(
            cast(DatabaseTransaction, transaction),
            "projector_savepoint",
        ),
    ):
        raise RuntimeError("private primary failure")

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "search.projection.transaction.rollback-only"
    assert "private" not in str(captured.value)
    assert "private" not in repr(captured.value)


class _CleanupFailingSavepoint:
    def __init__(self, inner: AbstractContextManager[None]) -> None:
        self._inner = inner

    def __enter__(self) -> None:
        self._inner.__enter__()

    def __exit__(self, *args: object) -> bool:
        del args
        raise RuntimeError("private savepoint cleanup failure")


class _RollbackOnly(Protocol):
    def mark_rollback_only(self) -> None: ...


class _CleanupFailingTransaction:
    def __init__(self, inner: DatabaseTransaction) -> None:
        self._inner = inner

    def execute(self, statement: DatabaseStatement, /) -> int:
        return self._inner.execute(statement)

    def query(self, statement: DatabaseStatement, /) -> tuple[DatabaseRow, ...]:
        return self._inner.query(statement)

    def stream(self, statement: DatabaseStatement, /) -> DatabaseCursor:
        return self._inner.stream(statement)

    def savepoint(self, name: str, /) -> AbstractContextManager[None]:
        return _CleanupFailingSavepoint(self._inner.savepoint(name))

    def mark_rollback_only(self) -> None:
        cast(_RollbackOnly, self._inner).mark_rollback_only()

    def claim_operation(self, name: str, /) -> bool:
        return self._inner.claim_operation(name)


class _SuppressingSavepoint:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> Literal[True]:
        del args
        return True


class _SuppressingTransaction(_CleanupFailingTransaction):
    def savepoint(self, name: str, /) -> _SuppressingSavepoint:
        del name
        return _SuppressingSavepoint()


def test_savepoint_cleanup_failure_marks_outer_transaction_rollback_only() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    target.upsert_error = RuntimeError("private target failure")

    with (
        pytest.raises(DatabaseOperationError) as captured_commit,
        database.transaction() as transaction,
    ):
        wrapped = _CleanupFailingTransaction(transaction)
        with pytest.raises(SearchProjectionError) as captured_apply:
            projector.apply(
                wrapped,
                _delivery(message_id="event-1", position=1),
            )

    assert captured_apply.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured_apply.value.stage == "search.projection.savepoint.cleanup"
    assert "private" not in str(captured_apply.value)
    assert "private" not in repr(captured_apply.value)
    assert captured_commit.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured_commit.value.stage == "database.transaction.rollback-only"
    assert _checkpoint(database, projector) is None
    database.close()


def test_suppressing_savepoint_cannot_leave_checkpoint_writes_committable() -> None:
    database, raw_target, projector = _setup()
    target = cast(RecordingTarget, raw_target)
    target.upsert_error = RuntimeError("private target failure")

    with (
        pytest.raises(DatabaseOperationError) as captured_commit,
        database.transaction() as transaction,
    ):
        wrapped = _SuppressingTransaction(transaction)
        with pytest.raises(SearchProjectionError) as captured_apply:
            projector.apply(
                wrapped,
                _delivery(message_id="event-1", position=1),
            )

    assert captured_apply.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured_apply.value.stage == "search.projection.savepoint.cleanup"
    assert captured_commit.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured_commit.value.stage == "database.transaction.rollback-only"
    assert _checkpoint(database, projector) is None
    database.close()


def test_persistent_restart_skips_committed_duplicate_without_replacing_content(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "projection-checkpoints.sqlite3"
    search_path = tmp_path / "search-index.sqlite3"
    original = _resource()
    delivery = _delivery(message_id="event-1", position=1, mutation=original)

    first_search = EmbeddedSearchDriver(
        search_path,
        physical_name=_GENERATION,
        projection_version=2,
    )
    first_database, _, first_projector = _setup(
        target=first_search,
        database_path=checkpoint_path,
    )
    with first_database.transaction() as transaction:
        assert first_projector.apply(transaction, delivery) is ProjectionDecision.APPLY
    first_database.close()
    first_search.close()

    second_search = EmbeddedSearchDriver(
        search_path,
        physical_name=_GENERATION,
        projection_version=2,
    )
    second_database, _, second_projector = _setup(
        target=second_search,
        database_path=checkpoint_path,
    )
    changed = replace(
        original,
        text="altered duplicate material",
        provenance=("sha256:" + "b" * 64,),
    )
    with second_database.transaction() as transaction:
        assert (
            second_projector.apply(
                transaction,
                _delivery(message_id="event-1", position=1, mutation=changed),
            )
            is ProjectionDecision.DUPLICATE
        )

    query = SearchQuery(
        text="original",
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
        ),
    )
    assert tuple(hit.resource_id for hit in second_search.search(query)) == ("resource-a",)
    assert not second_search.search(replace(query, text="altered"))
    checkpoint = _checkpoint(second_database, second_projector)
    assert checkpoint is not None
    assert checkpoint.position == 1
    assert checkpoint.message_id == "event-1"
    second_database.close()
    second_search.close()


def test_restored_target_cannot_reuse_a_checkpoint_for_missing_content(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "restore-checkpoints.sqlite3"
    search_path = tmp_path / "restore-search.sqlite3"
    snapshot_path = tmp_path / "restore-snapshot.sqlite3"
    delivery = _delivery(message_id="event-1", position=1)

    with EmbeddedSearchDriver(
        search_path,
        physical_name=_GENERATION,
        projection_version=2,
    ):
        pass
    copyfile(search_path, snapshot_path)

    first_search = EmbeddedSearchDriver(
        search_path,
        physical_name=_GENERATION,
        projection_version=2,
    )
    first_database, _, first_projector = _setup(
        target=first_search,
        database_path=checkpoint_path,
    )
    with first_database.transaction() as transaction:
        assert first_projector.apply(transaction, delivery) is ProjectionDecision.APPLY
    checkpoint_name = first_projector.checkpoint_name
    first_database.close()
    first_search.close()

    copyfile(snapshot_path, search_path)
    restored_search = EmbeddedSearchDriver(
        search_path,
        physical_name=_GENERATION,
        projection_version=2,
    )
    restored_database, _, restored_projector = _setup(
        target=restored_search,
        database_path=checkpoint_path,
    )
    assert restored_projector.checkpoint_name == checkpoint_name

    with (
        pytest.raises(SearchProjectionError) as captured,
        restored_database.transaction() as transaction,
    ):
        restored_projector.apply(transaction, delivery)

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert captured.value.stage == "search.projection.target-checkpoint"
    restored_database.close()
    restored_search.close()


def test_replaced_physical_store_gets_a_fresh_checkpoint_identity(
    tmp_path: Path,
) -> None:
    checkpoint_path = tmp_path / "replacement-checkpoints.sqlite3"
    delivery = _delivery(message_id="event-1", position=1)

    first_search = EmbeddedSearchDriver(
        tmp_path / "first-search.sqlite3",
        physical_name=_GENERATION,
        projection_version=2,
    )
    first_database, _, first_projector = _setup(
        target=first_search,
        database_path=checkpoint_path,
    )
    with first_database.transaction() as transaction:
        assert first_projector.apply(transaction, delivery) is ProjectionDecision.APPLY
    first_checkpoint_name = first_projector.checkpoint_name
    first_database.close()
    first_search.close()

    replacement_search = EmbeddedSearchDriver(
        tmp_path / "replacement-search.sqlite3",
        physical_name=_GENERATION,
        projection_version=2,
    )
    replacement_database, _, replacement_projector = _setup(
        target=replacement_search,
        database_path=checkpoint_path,
    )
    assert replacement_projector.checkpoint_name != first_checkpoint_name
    with replacement_database.transaction() as transaction:
        assert replacement_projector.apply(transaction, delivery) is ProjectionDecision.APPLY

    assert tuple(
        hit.resource_id
        for hit in replacement_search.search(
            SearchQuery(
                text="original",
                filters=(
                    SearchFilter("tenant_id", ("tenant-a",)),
                    SearchFilter("acl", ("group:research",)),
                ),
            )
        )
    ) == ("resource-a",)
    replacement_database.close()
    replacement_search.close()
