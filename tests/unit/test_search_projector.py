from __future__ import annotations

from datetime import UTC, datetime

import pytest

from af.framework.errors import PlatformReason
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import (
    ProjectionCheckpoint,
    ProjectionCheckpointStore,
    ProjectionDecision,
    ProjectionGapError,
)
from af.search.contracts import ResourceEnvelope, ResourceKind
from af.search.lifecycle import ResourceIdentity
from af.search.projector import (
    SearchProjectionDelivery,
    SearchProjectionError,
    SearchProjectionKind,
    SearchProjector,
)

_NOW = datetime(2026, 7, 26, 1, tzinfo=UTC)


def _resource(resource_id: str = "resource-a") -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=ResourceKind.TEXT,
        source_id="records",
        authoritative_locator=f"af://records/{resource_id}",
        tenant_id="tenant-a",
        projection_version=2,
        acl=("group:research",),
        provenance=("sha256:" + "a" * 64,),
        text="bounded searchable projection",
    )


def _upsert(
    *,
    message_id: str = "event-1",
    position: int = 1,
    resource_id: str = "resource-a",
) -> SearchProjectionDelivery:
    return SearchProjectionDelivery(
        projection_name="search.records",
        source_name="records.partition-0",
        message_id=message_id,
        position=position,
        processed_at=_NOW,
        kind=SearchProjectionKind.UPSERT,
        resource=_resource(resource_id),
        target_name="primary",
    )


def _delete(*, message_id: str, position: int) -> SearchProjectionDelivery:
    return SearchProjectionDelivery(
        projection_name="search.records",
        source_name="records.partition-0",
        message_id=message_id,
        position=position,
        processed_at=_NOW,
        kind=SearchProjectionKind.DELETE,
        identity=ResourceIdentity("tenant-a", "records", "resource-a"),
        target_name="primary",
    )


class RecordingSearch:
    def __init__(self) -> None:
        self.upserts: list[tuple[tuple[ResourceEnvelope, ...], str | None]] = []
        self.deletes: list[tuple[str, str, str, str | None]] = []
        self.fail_after_upsert = False
        self.upsert_count = 1
        self.delete_result: bool | object = False

    def upsert(
        self,
        resources: tuple[ResourceEnvelope, ...],
        *,
        name: str | None = None,
    ) -> int:
        self.upserts.append((resources, name))
        if self.fail_after_upsert:
            self.fail_after_upsert = False
            raise RuntimeError("secret transport acknowledgement")
        return self.upsert_count

    def delete(
        self,
        *,
        tenant_id: str,
        source_id: str,
        resource_id: str,
        name: str | None = None,
    ) -> bool:
        self.deletes.append((tenant_id, source_id, resource_id, name))
        return self.delete_result  # type: ignore[return-value]


def _projector(
    driver: SQLiteDriver,
    search: RecordingSearch,
) -> SearchProjector:
    projector = SearchProjector(driver.transaction, search)
    projector.install()
    return projector


def _checkpoint(driver: SQLiteDriver) -> ProjectionCheckpoint | None:
    with driver.transaction(read_only=True) as transaction:
        return ProjectionCheckpointStore().load(
            transaction,
            projection_name="search.records",
            source_name="records.partition-0",
        )


def test_projector_commits_acknowledged_upsert_and_skips_duplicate() -> None:
    driver = SQLiteDriver(":memory:")
    search = RecordingSearch()
    projector = _projector(driver, search)
    delivery = _upsert()

    assert projector.project(delivery) is ProjectionDecision.APPLY
    assert projector.project(delivery) is ProjectionDecision.DUPLICATE

    assert len(search.upserts) == 1
    assert search.upserts[0] == (((delivery.resource,), "primary"))
    checkpoint = _checkpoint(driver)
    assert checkpoint is not None
    assert checkpoint.position == 1
    assert checkpoint.message_id == "event-1"
    driver.close()


def test_lost_search_acknowledgement_rolls_back_checkpoint_and_replays() -> None:
    driver = SQLiteDriver(":memory:")
    search = RecordingSearch()
    search.fail_after_upsert = True
    projector = _projector(driver, search)
    delivery = _upsert()

    with pytest.raises(SearchProjectionError) as captured:
        projector.project(delivery)

    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert "secret" not in str(captured.value)
    assert _checkpoint(driver) is None

    assert projector.project(delivery) is ProjectionDecision.APPLY
    assert len(search.upserts) == 2
    assert _checkpoint(driver) is not None
    driver.close()


def test_projector_commits_idempotent_tombstone_and_contiguous_checkpoint() -> None:
    driver = SQLiteDriver(":memory:")
    search = RecordingSearch()
    projector = _projector(driver, search)

    assert projector.project(_upsert()) is ProjectionDecision.APPLY
    assert projector.project(_delete(message_id="event-2", position=2)) is ProjectionDecision.APPLY

    assert search.deletes == [("tenant-a", "records", "resource-a", "primary")]
    checkpoint = _checkpoint(driver)
    assert checkpoint is not None
    assert checkpoint.position == 2
    driver.close()


def test_stale_and_gap_deliveries_cannot_mutate_search() -> None:
    driver = SQLiteDriver(":memory:")
    search = RecordingSearch()
    projector = _projector(driver, search)
    assert projector.project(_upsert(message_id="event-2", position=2)) is ProjectionDecision.APPLY

    stale = _upsert(message_id="event-1", position=1, resource_id="resource-stale")
    assert projector.project(stale) is ProjectionDecision.STALE
    with pytest.raises(ProjectionGapError):
        projector.project(_upsert(message_id="event-4", position=4, resource_id="resource-gap"))

    assert len(search.upserts) == 1
    assert _checkpoint(driver).position == 2  # type: ignore[union-attr]
    driver.close()


@pytest.mark.parametrize("count", [0, 2, True])
def test_non_exhaustive_upsert_acknowledgement_fails_closed(count: int) -> None:
    driver = SQLiteDriver(":memory:")
    search = RecordingSearch()
    search.upsert_count = count
    projector = _projector(driver, search)

    with pytest.raises(SearchProjectionError) as captured:
        projector.project(_upsert())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert _checkpoint(driver) is None
    driver.close()


def test_non_boolean_tombstone_acknowledgement_fails_closed() -> None:
    driver = SQLiteDriver(":memory:")
    search = RecordingSearch()
    search.delete_result = 1
    projector = _projector(driver, search)

    with pytest.raises(SearchProjectionError) as captured:
        projector.project(_delete(message_id="event-1", position=1))

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert _checkpoint(driver) is None
    driver.close()


def test_projection_delivery_requires_exactly_one_canonical_effect() -> None:
    identity = ResourceIdentity("tenant-a", "records", "resource-a")
    with pytest.raises(ValueError):
        SearchProjectionDelivery(
            projection_name="search.records",
            source_name="records.partition-0",
            message_id="event-1",
            position=1,
            processed_at=_NOW,
            kind=SearchProjectionKind.UPSERT,
            resource=_resource(),
            identity=identity,
        )
    with pytest.raises(ValueError):
        SearchProjectionDelivery(
            projection_name="search.records",
            source_name="records.partition-0",
            message_id="event-1",
            position=1,
            processed_at=_NOW,
            kind=SearchProjectionKind.DELETE,
        )
    with pytest.raises(ValueError):
        SearchProjectionDelivery(
            projection_name="INVALID NAME",
            source_name="records.partition-0",
            message_id="event-1",
            position=1,
            processed_at=_NOW,
            kind=SearchProjectionKind.UPSERT,
            resource=_resource(),
        )
