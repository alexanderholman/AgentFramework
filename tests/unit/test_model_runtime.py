from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event

import pytest

from af.application.model_runtime import (
    ModelRuntimeBroker,
    RuntimeHealth,
    RuntimeInvocation,
    RuntimeRefused,
    RuntimeState,
    RuntimeVariant,
)
from af.application.resource_leases import ResourceLeaseService
from af.domain.resources import LeaseState, ResourceBudget, ResourceIdentity, ResourceKind
from af.infrastructure.resource_leases import InMemoryLeaseRepository

DIGEST = "sha256:" + "a" * 64


class LocalAdapter:
    def __init__(self) -> None:
        self.loaded: list[str] = []
        self.unloaded: list[str] = []
        self.loading = Event()
        self.continue_load = Event()
        self.continue_load.set()
        self.fail = False
        self.ready = True

    def load(self, variant: RuntimeVariant, cancellation: Event, deadline: datetime) -> None:
        self.loading.set()
        assert self.continue_load.wait(timeout=3)
        if self.fail:
            raise RuntimeError("runtime failed")
        self.loaded.append(variant.variant_id)

    def health(self, variant: RuntimeVariant) -> RuntimeHealth:
        return RuntimeHealth(True, True, True, self.ready, variant.digest)

    def unload(self, variant: RuntimeVariant) -> None:
        self.unloaded.append(variant.variant_id)


def invocation(name: str = "a", *, variant_id: str = "qwen.q4") -> RuntimeInvocation:
    return RuntimeInvocation(
        invocation_id=name,
        variant=RuntimeVariant(variant_id, DIGEST, DIGEST, DIGEST, DIGEST, 100),
        policy_digest=DIGEST,
        scope_digest=DIGEST,
        authorized_by="owner",
        authorization_digest=DIGEST,
        deadline=datetime.now(UTC) + timedelta(seconds=10),
        authorized_until=datetime.now(UTC) + timedelta(minutes=10),
        cancellation=Event(),
    )


def broker(
    adapter: LocalAdapter,
    *,
    max_queue: int = 16,
) -> tuple[ModelRuntimeBroker, InMemoryLeaseRepository]:
    repository = InMemoryLeaseRepository()
    return ModelRuntimeBroker(
        adapter,
        ResourceLeaseService(repository),
        ResourceBudget(
            ResourceIdentity(ResourceKind.RAM, "host", "memory"),
            1000,
            max_duration=timedelta(minutes=5),
            max_concurrent=1,
        ),
        authorize=lambda request: True,
        max_queue=max_queue,
    ), repository


def test_exact_variant_cold_warm_swap_and_shutdown_retain_exclusive_lease() -> None:
    adapter = LocalAdapter()
    runtime, repository = broker(adapter)
    request = invocation()
    assert runtime.execute(request, lambda: "first") == "first"
    assert runtime.state is RuntimeState.WARM
    assert [lease.state for lease in repository.list()] == [LeaseState.ACTIVE]
    assert runtime.execute(invocation("b"), lambda: "second") == "second"
    assert adapter.loaded == ["qwen.q4"]
    runtime.execute(invocation("c", variant_id="qwen.q8"), lambda: None)
    assert adapter.unloaded == ["qwen.q4"]
    assert adapter.loaded == ["qwen.q4", "qwen.q8"]
    assert [lease.state for lease in repository.list()].count(LeaseState.ACTIVE) == 1
    runtime.shutdown()
    assert runtime.state is RuntimeState.COLD
    assert all(lease.state is LeaseState.RELEASED for lease in repository.list())
    assert RuntimeState.LOADING in [transition.state for transition in runtime.transitions]


def test_concurrent_cold_requests_coalesce_to_one_load() -> None:
    adapter = LocalAdapter()
    adapter.continue_load.clear()
    runtime, _ = broker(adapter)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(runtime.execute, invocation("one"), lambda: "1")
        assert adapter.loading.wait(timeout=2)
        second = workers.submit(runtime.execute, invocation("two"), lambda: "2")
        adapter.continue_load.set()
        assert first.result(timeout=3) == "1"
        assert second.result(timeout=3) == "2"
    assert adapter.loaded == ["qwen.q4"]
    runtime.shutdown()


def test_failed_or_unready_load_never_emits_warm_and_releases_capacity() -> None:
    for fail in (True, False):
        adapter = LocalAdapter()
        adapter.fail = fail
        adapter.ready = False
        runtime, repository = broker(adapter)
        with pytest.raises(RuntimeRefused):
            runtime.execute(invocation(), lambda: pytest.fail("work must not execute"))
        assert RuntimeState.WARM not in [event.state for event in runtime.transitions]
        assert all(lease.state is LeaseState.RELEASED for lease in repository.list())
        assert runtime.state is RuntimeState.FAILED


def test_cancelled_load_releases_lease_and_never_dispatches_work() -> None:
    adapter = LocalAdapter()
    adapter.continue_load.clear()
    runtime, repository = broker(adapter)
    request = invocation()
    with ThreadPoolExecutor(max_workers=1) as workers:
        result = workers.submit(runtime.execute, request, lambda: pytest.fail("cancelled"))
        assert adapter.loading.wait(timeout=2)
        request.cancellation.set()
        adapter.continue_load.set()
        with pytest.raises(RuntimeRefused, match="cancelled"):
            result.result(timeout=3)
    assert all(lease.state is LeaseState.RELEASED for lease in repository.list())


def test_revocation_checked_before_load_and_before_result_acceptance() -> None:
    adapter = LocalAdapter()
    runtime, repository = broker(adapter)
    allowed = True
    runtime.authorize = lambda request: allowed

    def revoke() -> str:
        nonlocal allowed
        allowed = False
        return "must not accept"

    with pytest.raises(RuntimeRefused, match="authorization_revoked"):
        runtime.execute(invocation(), revoke)
    assert all(lease.state is LeaseState.RELEASED for lease in repository.list())
    with pytest.raises(RuntimeRefused, match="authorization_revoked"):
        runtime.execute(invocation("another"), lambda: None)
    assert adapter.loaded == ["qwen.q4"]


def test_revoked_idle_residency_drains_before_a_new_request_is_refused() -> None:
    adapter = LocalAdapter()
    runtime, repository = broker(adapter)
    runtime.execute(invocation(), lambda: None)
    runtime.authorize = lambda request: False
    with pytest.raises(RuntimeRefused, match="authorization_revoked"):
        runtime.execute(invocation("another"), lambda: None)
    assert all(item.state is LeaseState.RELEASED for item in repository.list())
    assert runtime.state is RuntimeState.COLD


def test_deadline_queue_limit_and_changed_scope_fail_closed() -> None:
    adapter = LocalAdapter()
    runtime, repository = broker(adapter)
    with pytest.raises(RuntimeRefused, match="deadline_exceeded"):
        runtime.execute(replace(invocation(), deadline=datetime.now(UTC)), lambda: None)
    assert not repository.list()
    runtime.execute(invocation(), lambda: None)
    changed = replace(invocation("b"), scope_digest="sha256:" + "b" * 64)
    runtime.execute(changed, lambda: None)
    assert len(adapter.loaded) == 2
    runtime.shutdown()


def test_revoked_residency_lease_is_not_reused() -> None:
    adapter = LocalAdapter()
    runtime, repository = broker(adapter)
    runtime.execute(invocation(), lambda: None)
    lease = repository.list()[0]
    assert lease.fence_token is not None
    ResourceLeaseService(repository).revoke_lease(lease.id, lease.fence_token)
    with pytest.raises(RuntimeRefused, match="lease_invalid"):
        runtime.execute(invocation("b"), lambda: None)
    assert adapter.loaded == ["qwen.q4"]


def test_start_limit_quarantines_and_explicit_reset_restores_service() -> None:
    adapter = LocalAdapter()
    adapter.fail = True
    runtime, _ = broker(adapter)
    for _ in range(3):
        with pytest.raises(RuntimeRefused):
            runtime.execute(invocation(), lambda: None)
    assert runtime.state is RuntimeState.QUARANTINED
    assert runtime.transitions[-1].to_dict()["reason_code"] == "start_limit_exhausted"
    with pytest.raises(RuntimeRefused, match="runtime_quarantined"):
        runtime.execute(invocation(), lambda: None)
    adapter.fail = False
    runtime.reset()
    assert runtime.execute(invocation(), lambda: 7) == 7
    runtime.shutdown()
    with pytest.raises(RuntimeRefused, match="broker_closed"):
        runtime.execute(invocation(), lambda: None)


def test_idle_maintenance_respects_pin_but_not_expired_authorization() -> None:
    adapter = LocalAdapter()
    repository = InMemoryLeaseRepository()
    now = datetime.now(UTC)
    runtime = ModelRuntimeBroker(
        adapter,
        ResourceLeaseService(repository, clock=lambda: now),
        ResourceBudget(
            ResourceIdentity(ResourceKind.RAM, "h", "r"),
            1000,
            max_duration=timedelta(minutes=5),
            max_concurrent=1,
        ),
        authorize=lambda request: True,
        clock=lambda: now,
        idle_seconds=1,
    )
    runtime.maintain()
    with pytest.raises(RuntimeRefused, match="variant_not_resident"):
        runtime.pin(DIGEST)
    request = invocation()
    runtime.execute(request, lambda: None)
    runtime.pin(request.variant.digest)
    now += timedelta(seconds=2)
    runtime.maintain()
    assert runtime.state is RuntimeState.WARM
    runtime.pin(request.variant.digest, pinned=False)
    runtime.maintain()
    assert runtime.state is RuntimeState.COLD
    runtime.execute(invocation("b"), lambda: None)
    now += timedelta(minutes=11)
    runtime.maintain()
    assert runtime.state is RuntimeState.COLD


def test_bounded_fifo_cancellation_duplicate_request_and_shutdown() -> None:
    adapter = LocalAdapter()
    adapter.continue_load.clear()
    runtime, repository = broker(adapter, max_queue=1)
    second_entered = Event()

    def authorize(request: RuntimeInvocation) -> bool:
        if request.invocation_id == "two":
            second_entered.set()
        return True

    runtime.authorize = authorize
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(runtime.execute, invocation("one"), lambda: None)
        assert adapter.loading.wait(timeout=2)
        with pytest.raises(RuntimeRefused, match="duplicate_invocation"):
            runtime.execute(invocation("one"), lambda: None)
        second_request = invocation("two")
        second = workers.submit(runtime.execute, second_request, lambda: None)
        assert second_entered.wait(timeout=2)
        with pytest.raises(RuntimeRefused, match="queue_full"):
            runtime.execute(invocation("three"), lambda: None)
        with pytest.raises(RuntimeRefused, match="broker_busy"):
            runtime.reset()
        runtime.maintain()
        second_request.cancellation.set()
        with pytest.raises(RuntimeRefused, match="cancelled"):
            second.result(timeout=2)
        with pytest.raises(RuntimeRefused, match="shutdown_timeout"):
            runtime.shutdown(timeout=0)
        adapter.continue_load.set()
        with pytest.raises(RuntimeRefused):
            first.result(timeout=2)
    assert all(item.state is LeaseState.RELEASED for item in repository.list())


def test_failed_health_after_work_discards_result_and_unload_failure_keeps_lease() -> None:
    adapter = LocalAdapter()
    runtime, repository = broker(adapter)

    def corrupt_health() -> str:
        adapter.ready = False
        return "unverified"

    with pytest.raises(RuntimeRefused, match="runtime_health_failed"):
        runtime.execute(invocation(), corrupt_health)
    assert all(item.state is LeaseState.RELEASED for item in repository.list())

    class CannotUnload(LocalAdapter):
        def unload(self, variant: RuntimeVariant) -> None:
            raise RuntimeError("process still running")

    runtime, repository = broker(CannotUnload())
    runtime.execute(invocation(), lambda: None)
    with pytest.raises(RuntimeRefused, match="unload_failed"):
        runtime.shutdown()
    assert runtime.state is RuntimeState.QUARANTINED
    assert repository.list()[0].state is LeaseState.ACTIVE


@pytest.mark.parametrize(
    "change",
    [
        {"variant_id": "INVALID ID"},
        {"artifact_digest": "digest"},
        {"memory_mib": -1},
    ],
)
def test_runtime_variant_rejects_unpinned_or_unbounded_identity(change: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(invocation().variant, **change)


def test_runtime_requires_expiring_exclusive_capacity_and_valid_times() -> None:
    request = invocation()
    for change in (
        {"invocation_id": ""},
        {"deadline": datetime(2026, 1, 1)},
        {"cancellation": None},
    ):
        with pytest.raises(ValueError):
            replace(request, **change)
    runtime, _ = broker(LocalAdapter())
    with pytest.raises(ValueError):
        runtime.shutdown(timeout=-1)
    for max_queue, duration in ((0, timedelta(seconds=1)), (1, None)):
        with pytest.raises(ValueError):
            ModelRuntimeBroker(
                LocalAdapter(),
                ResourceLeaseService(InMemoryLeaseRepository()),
                ResourceBudget(
                    ResourceIdentity(ResourceKind.RAM, "h", "r"),
                    1000,
                    max_duration=duration,
                    max_concurrent=1,
                ),
                authorize=lambda request: True,
                max_queue=max_queue,
            )
