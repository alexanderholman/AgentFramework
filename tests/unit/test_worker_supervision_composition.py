from __future__ import annotations

from datetime import timedelta
from threading import Event
from typing import Never

import pytest

from af.bootstrap import create_application
from af.config import Settings
from af.domain.resources import Lease
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.resource_leases import InMemoryLeaseRepository
from af.infrastructure.tracing import InMemoryTraceRepository
from af.observability.signals import TelemetryPublisher, TelemetrySignal


class _RecordingLeaseRepository(InMemoryLeaseRepository):
    def __init__(self) -> None:
        super().__init__()
        self.list_host_ids: list[str | None] = []

    def list(
        self,
        host_id: str | None = None,
        state: str | None = None,
    ) -> list[Lease]:
        self.list_host_ids.append(host_id)
        return super().list(host_id=host_id, state=state)


class _RecordingSink:
    def __init__(self) -> None:
        self.signals: list[TelemetrySignal] = []

    def emit(self, signal: TelemetrySignal, /) -> None:
        self.signals.append(signal)


def test_bootstrap_composes_configured_lease_maintenance_before_runtime_factory() -> None:
    repository = _RecordingLeaseRepository()
    application = create_application(
        settings=Settings(
            resource_lease_maintenance_host_id="worker-a.example",
            resource_lease_maintenance_interval_seconds=2.5,
        ),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        lease_repository=repository,
    )
    sink = _RecordingSink()
    runtime_failure = RuntimeError("runtime factory stopped after maintenance")
    runtime_factory_calls = 0

    def runtime_factory() -> Never:
        nonlocal runtime_factory_calls
        runtime_factory_calls += 1
        raise runtime_failure

    try:
        factory = application.worker_supervisors
        assert factory.host_id == "worker-a.example"
        assert factory.maintenance_interval == timedelta(seconds=2.5)
        supervisor = factory.create(
            runtime_factory,
            telemetry=TelemetryPublisher((sink,)),
            max_restarts=0,
        )

        with pytest.raises(RuntimeError) as captured:
            supervisor.run(stop_event=Event())

        assert captured.value is runtime_failure
        assert repository.list_host_ids == ["worker-a.example"]
        assert runtime_factory_calls == 1
        assert supervisor.maintenance_run_count == 1
        assert len(sink.signals) == 1
        assert dict(sink.signals[0].attributes) == {
            "conflicted": "none",
            "expired": "none",
            "reconciled": "none",
            "status": "clean",
        }
    finally:
        application.shutdown()
