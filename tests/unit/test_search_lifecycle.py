from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from af.framework.errors import PlatformReason
from af.search.contracts import ResourceEnvelope, ResourceKind
from af.search.lifecycle import (
    BulkIndexFailure,
    BulkIndexResult,
    IndexBuild,
    IndexLifecycleError,
    IndexLifecycleManager,
    IndexPhase,
    IndexStatus,
    ResourceIdentity,
)

_NOW = datetime(2026, 7, 24, 15, tzinfo=UTC)


def _resource(resource_id: str, *, projection_version: int = 2) -> ResourceEnvelope:
    return ResourceEnvelope(
        resource_id=resource_id,
        kind=ResourceKind.TEXT,
        source_id="source-primary",
        authoritative_locator=f"af://source-primary/{resource_id}",
        tenant_id="tenant-a",
        projection_version=projection_version,
        acl=("group:research",),
        provenance=("sha256:" + resource_id[-1] * 64,),
        text="bounded searchable energy content",
    )


class RecordingBackend:
    def __init__(self, *, active: str | None = "catalog-g00000001") -> None:
        self.aliases: dict[str, str | None] = {"catalog": active}
        self.indices: set[str] = {active} if active is not None else set()
        self.indexed: dict[str, list[ResourceEnvelope]] = {}
        self.deleted: list[str] = []
        self.partial_failure = False
        self.force_alias_conflict = False

    def create_index(self, name: str, *, projection_version: int) -> None:
        del projection_version
        if name in self.indices:
            raise RuntimeError("sensitive duplicate index")
        self.indices.add(name)

    def bulk_upsert(
        self,
        name: str,
        resources: tuple[ResourceEnvelope, ...],
    ) -> BulkIndexResult:
        identities = tuple(ResourceIdentity.from_resource(resource) for resource in resources)
        if self.partial_failure:
            return BulkIndexResult(
                attempted=identities,
                succeeded=identities[:-1],
                failures=(BulkIndexFailure(identities[-1], "backend-rejected"),),
            )
        self.indexed.setdefault(name, []).extend(resources)
        return BulkIndexResult(
            attempted=identities,
            succeeded=identities,
        )

    def delete_index(self, name: str) -> None:
        self.indices.remove(name)
        self.deleted.append(name)

    def active_index(self, alias: str) -> str | None:
        return self.aliases.get(alias)

    def compare_and_swap_alias(
        self,
        alias: str,
        *,
        expected: str | None,
        replacement: str,
    ) -> bool:
        if self.force_alias_conflict or self.aliases.get(alias) != expected:
            return False
        self.aliases[alias] = replacement
        return True


def test_rebuild_batches_cut_over_and_roll_back_atomically() -> None:
    backend = RecordingBackend()
    manager = IndexLifecycleManager(backend, clock=lambda: _NOW)
    build = manager.begin("catalog", projection_version=2)

    first = (_resource("resource-a"), _resource("resource-b"))
    second = (_resource("resource-c"),)
    assert manager.apply(build, first).complete
    assert manager.apply(build, second).complete
    ready = manager.mark_ready(build, checkpoint="event-0003")

    assert ready.phase is IndexPhase.READY
    assert ready.indexed_resources == 3
    assert manager.cutover(build).phase is IndexPhase.ACTIVE
    assert backend.aliases["catalog"] == build.physical_name

    rolled_back = manager.rollback("catalog")

    assert rolled_back == "catalog-g00000001"
    assert backend.aliases["catalog"] == "catalog-g00000001"
    assert manager.status(build).phase is IndexPhase.RETIRED


def test_partial_bulk_failure_blocks_cutover_and_preserves_current_alias() -> None:
    backend = RecordingBackend()
    backend.partial_failure = True
    manager = IndexLifecycleManager(backend, clock=lambda: _NOW)
    build = manager.begin("catalog", projection_version=2)

    result = manager.apply(build, (_resource("resource-a"), _resource("resource-b")))

    assert not result.complete
    assert result.failures[0].reason == "backend-rejected"
    assert manager.status(build).phase is IndexPhase.FAILED
    with pytest.raises(IndexLifecycleError) as captured:
        manager.mark_ready(build, checkpoint="event-0002")
    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert backend.aliases["catalog"] == "catalog-g00000001"


def test_alias_drift_fences_stale_cutover() -> None:
    backend = RecordingBackend()
    manager = IndexLifecycleManager(backend, clock=lambda: _NOW)
    build = manager.begin("catalog", projection_version=2)
    manager.apply(build, (_resource("resource-a"),))
    manager.mark_ready(build, checkpoint="event-0001")
    backend.aliases["catalog"] = "catalog-g99999999"

    with pytest.raises(IndexLifecycleError) as captured:
        manager.cutover(build)

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert manager.status(build).phase is IndexPhase.FAILED
    assert backend.aliases["catalog"] == "catalog-g99999999"


def test_stale_recovery_deletes_only_abandoned_nonactive_generations() -> None:
    current = [_NOW]
    backend = RecordingBackend()
    manager = IndexLifecycleManager(
        backend,
        clock=lambda: current[0],
        stale_after=timedelta(minutes=5),
    )
    stale = manager.begin("catalog", projection_version=2)
    current[0] += timedelta(minutes=6)
    fresh = manager.begin("catalog", projection_version=3)

    recovered = manager.recover_stale("catalog")

    assert recovered == (stale.physical_name,)
    assert stale.physical_name in backend.deleted
    assert fresh.physical_name in backend.indices
    assert "catalog-g00000001" in backend.indices
    with pytest.raises(IndexLifecycleError):
        manager.status(stale)


def test_backend_result_must_correspond_exactly_to_requested_batch() -> None:
    class CorruptBackend(RecordingBackend):
        def bulk_upsert(
            self,
            name: str,
            resources: tuple[ResourceEnvelope, ...],
        ) -> BulkIndexResult:
            del name, resources
            identity = ResourceIdentity("tenant-a", "source-primary", "resource-other")
            return BulkIndexResult(attempted=(identity,), succeeded=(identity,))

    manager = IndexLifecycleManager(CorruptBackend(), clock=lambda: _NOW)
    build = manager.begin("catalog", projection_version=2)

    with pytest.raises(IndexLifecycleError) as captured:
        manager.apply(build, (_resource("resource-a"),))

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert str(captured.value) == "integrity validation failed"
    assert manager.status(build).phase is IndexPhase.FAILED


def test_lifecycle_handles_and_bulk_results_fail_closed() -> None:
    identity = ResourceIdentity("tenant-a", "source-primary", "resource-a")
    with pytest.raises(ValueError):
        BulkIndexResult(
            attempted=(identity,),
            succeeded=(identity,),
            failures=(BulkIndexFailure(identity, "duplicate"),),
        )
    with pytest.raises(ValueError):
        IndexBuild(
            logical_name="catalog",
            physical_name="other-g00000001",
            generation=1,
            projection_version=1,
            base_index=None,
            started_at=_NOW,
        )


def test_lifecycle_validates_clocks_intervals_batches_and_projection_versions() -> None:
    with pytest.raises(ValueError):
        IndexLifecycleManager(RecordingBackend(), stale_after=timedelta(0))
    with pytest.raises(ValueError):
        IndexLifecycleManager(
            RecordingBackend(),
            clock=lambda: datetime(2026, 7, 24),
        ).begin("catalog", projection_version=2)
    with pytest.raises(ValueError):
        IndexLifecycleManager(RecordingBackend()).begin("catalog", projection_version=0)

    manager = IndexLifecycleManager(RecordingBackend(), clock=lambda: _NOW)
    build = manager.begin("catalog", projection_version=2)
    with pytest.raises(ValueError):
        manager.apply(build, ())
    with pytest.raises(ValueError):
        manager.apply(build, (_resource("resource-a"), _resource("resource-a")))
    with pytest.raises(ValueError):
        manager.apply(build, (_resource("resource-a", projection_version=3),))
    with pytest.raises(TypeError):
        ResourceIdentity.from_resource(object())  # type: ignore[arg-type]


def test_backend_create_and_bulk_failures_are_sanitized() -> None:
    class ObserveFailure(RecordingBackend):
        def active_index(self, alias: str) -> str | None:
            del alias
            raise RuntimeError("sensitive alias failure")

    class CreateFailure(RecordingBackend):
        def create_index(self, name: str, *, projection_version: int) -> None:
            del name, projection_version
            raise RuntimeError("sensitive create failure")

    with pytest.raises(IndexLifecycleError) as observed:
        IndexLifecycleManager(ObserveFailure(), clock=lambda: _NOW).begin(
            "catalog", projection_version=2
        )
    with pytest.raises(IndexLifecycleError) as created:
        IndexLifecycleManager(CreateFailure(), clock=lambda: _NOW).begin(
            "catalog", projection_version=2
        )
    assert observed.value.reason is PlatformReason.PERMANENT_FAILURE
    assert created.value.reason is PlatformReason.PERMANENT_FAILURE
    assert "sensitive" not in str(observed.value)
    assert "sensitive" not in str(created.value)

    class BulkFailure(RecordingBackend):
        def bulk_upsert(
            self,
            name: str,
            resources: tuple[ResourceEnvelope, ...],
        ) -> BulkIndexResult:
            del name, resources
            raise RuntimeError("sensitive bulk failure")

    manager = IndexLifecycleManager(BulkFailure(), clock=lambda: _NOW)
    build = manager.begin("catalog", projection_version=2)
    with pytest.raises(IndexLifecycleError) as bulk:
        manager.apply(build, (_resource("resource-a"),))
    assert bulk.value.reason is PlatformReason.PERMANENT_FAILURE
    assert manager.status(build).phase is IndexPhase.FAILED


def test_cutover_and_rollback_backend_fences_fail_closed() -> None:
    backend = RecordingBackend()
    manager = IndexLifecycleManager(backend, clock=lambda: _NOW)
    with pytest.raises(IndexLifecycleError):
        manager.rollback("catalog")

    build = manager.begin("catalog", projection_version=2)
    manager.apply(build, (_resource("resource-a"),))
    manager.mark_ready(build, checkpoint="event-0001")
    backend.force_alias_conflict = True
    with pytest.raises(IndexLifecycleError) as cutover:
        manager.cutover(build)
    assert cutover.value.reason is PlatformReason.INTEGRITY_FAILURE

    backend = RecordingBackend()
    manager = IndexLifecycleManager(backend, clock=lambda: _NOW)
    build = manager.begin("catalog", projection_version=2)
    manager.apply(build, (_resource("resource-a"),))
    manager.mark_ready(build, checkpoint="event-0001")
    manager.cutover(build)
    backend.aliases["catalog"] = "catalog-g99999999"
    with pytest.raises(IndexLifecycleError) as rollback:
        manager.rollback("catalog")
    assert rollback.value.reason is PlatformReason.INTEGRITY_FAILURE


def test_rebuild_of_managed_generation_retires_and_restores_prior_state() -> None:
    backend = RecordingBackend(active=None)
    manager = IndexLifecycleManager(backend, clock=lambda: _NOW)
    first = manager.begin("catalog", projection_version=1)
    manager.apply(first, (_resource("resource-a", projection_version=1),))
    manager.mark_ready(first, checkpoint="event-0001")
    manager.cutover(first)

    second = manager.begin("catalog", projection_version=2)
    manager.apply(second, (_resource("resource-b"),))
    manager.mark_ready(second, checkpoint="event-0002")
    manager.cutover(second)

    assert manager.status(first).phase is IndexPhase.RETIRED
    assert manager.rollback("catalog") == first.physical_name
    assert manager.status(first).phase is IndexPhase.ACTIVE
    assert manager.status(second).phase is IndexPhase.RETIRED


def test_recovery_observation_and_deletion_failures_are_sanitized() -> None:
    current = [_NOW]
    backend = RecordingBackend()
    manager = IndexLifecycleManager(
        backend,
        clock=lambda: current[0],
        stale_after=timedelta(minutes=1),
    )
    build = manager.begin("catalog", projection_version=2)
    current[0] += timedelta(minutes=2)

    class DeleteFailureBackend(RecordingBackend):
        def delete_index(self, name: str) -> None:
            del name
            raise RuntimeError("sensitive deletion failure")

    failing = DeleteFailureBackend()
    failing_manager = IndexLifecycleManager(
        failing,
        clock=lambda: current[0],
        stale_after=timedelta(minutes=1),
    )
    failing_build = failing_manager.begin("catalog", projection_version=2)
    current[0] += timedelta(minutes=2)
    with pytest.raises(IndexLifecycleError) as deleted:
        failing_manager.recover_stale("catalog")
    assert deleted.value.reason is PlatformReason.PERMANENT_FAILURE
    assert failing_manager.status(failing_build).phase is IndexPhase.BUILDING

    backend.aliases["catalog"] = build.physical_name
    assert manager.recover_stale("catalog") == ()


def test_status_and_value_contracts_reject_invalid_state() -> None:
    identity = ResourceIdentity("tenant-a", "source-primary", "resource-a")
    with pytest.raises(ValueError):
        ResourceIdentity("not a tenant", "source-primary", "resource-a")
    with pytest.raises(ValueError):
        BulkIndexFailure(identity, "not a reason")
    with pytest.raises(ValueError):
        BulkIndexResult(attempted=(identity, identity))
    with pytest.raises(ValueError):
        IndexStatus(
            build=object(),  # type: ignore[arg-type]
            phase=IndexPhase.BUILDING,
            indexed_resources=0,
            checkpoint=None,
            updated_at=_NOW,
        )

    manager = IndexLifecycleManager(RecordingBackend(), clock=lambda: _NOW)
    unknown = IndexBuild(
        logical_name="catalog",
        physical_name="catalog-g00000002",
        generation=2,
        projection_version=2,
        base_index="catalog-g00000001",
        started_at=_NOW,
    )
    with pytest.raises(IndexLifecycleError):
        manager.status(unknown)
