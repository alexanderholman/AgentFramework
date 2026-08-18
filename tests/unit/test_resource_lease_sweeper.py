from __future__ import annotations

from datetime import UTC, datetime, timedelta

from af.application.resource_leases import ResourceLeaseService, ResourceLeaseSweepMaintenance
from af.domain.resources import LeaseState, ResourceBudget, ResourceIdentity, ResourceKind
from af.infrastructure.resource_leases import InMemoryLeaseRepository
from af.observability.signals import TelemetryKind, TelemetryPublisher, TelemetrySignal


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class _RecordingSink:
    def __init__(self) -> None:
        self.signals: list[TelemetrySignal] = []

    def emit(self, signal: TelemetrySignal, /) -> None:
        self.signals.append(signal)


def test_resource_lease_maintenance_exposes_exact_result_and_bounded_telemetry() -> None:
    started_at = datetime(2026, 8, 18, 3, tzinfo=UTC)
    clock = _Clock(started_at)
    service = ResourceLeaseService(InMemoryLeaseRepository(), clock=clock)
    budget = ResourceBudget(
        identity=ResourceIdentity(
            kind=ResourceKind.CONCURRENCY,
            host_id="host.local",
            instance_id="worker.slots",
        ),
        max_quantity=1,
        max_duration=timedelta(minutes=1),
        max_concurrent=1,
    )
    reserved = service.create_lease(
        "lease-maintenance",
        budget,
        1,
        {"queue": "default"},
        "worker-policy",
        "authorization-v1",
    )
    assert reserved.fence_token is not None
    active = service.activate_lease(reserved.id, reserved.fence_token)
    sink = _RecordingSink()
    maintenance = ResourceLeaseSweepMaintenance(
        service,
        host_id="host.local",
        telemetry=TelemetryPublisher((sink,)),
        clock=clock,
    )
    clock.current = started_at + timedelta(minutes=1)

    maintenance.run()

    assert maintenance.run_count == 1
    assert maintenance.last_completed_at == clock.current
    result = maintenance.last_result
    assert result is not None
    assert result.expired_lease_ids == (active.id,)
    assert result.reconciled_lease_ids == (active.id,)
    assert result.conflicted_lease_ids == ()
    assert len(sink.signals) == 1
    signal = sink.signals[0]
    assert signal.kind is TelemetryKind.METRIC
    assert signal.name == "resource.lease.sweep"
    assert dict(signal.attributes) == {
        "conflicted": "none",
        "expired": "one",
        "reconciled": "one",
        "status": "reconciled",
    }
    assert service.get_lease(active.id).state is LeaseState.RECONCILED

    maintenance.run()

    assert maintenance.run_count == 2
    assert dict(sink.signals[1].attributes) == {
        "conflicted": "none",
        "expired": "none",
        "reconciled": "none",
        "status": "clean",
    }
