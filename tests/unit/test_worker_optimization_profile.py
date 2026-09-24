from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path

import pytest

from af.application.worker_generation import WorkerGenerationFactory
from af.bootstrap import create_application
from af.config import Settings
from af.framework.errors import ApplicationBootError, ServiceNotBound
from af.framework.managers import CapabilityBinding
from af.framework.programs import ProgramBudget, ProgramRequest
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.sqlite import SQLiteDriver
from af.infrastructure.tracing import InMemoryTraceRepository
from af.infrastructure.worker_optimization import worker_queue_storage_identity
from af.queue.database_manager import database_queue_registration
from af.queue.manager import QueueManager


def _binding(queue_path: Path) -> CapabilityBinding:
    storage_identity = worker_queue_storage_identity(queue_path)
    manager = QueueManager(
        connections={
            "primary": {
                "driver": "database",
                "storage_identity": storage_identity,
            }
        },
        default="primary",
        deployment_profile="local",
        policy_version="af-worker-v1",
    )
    manager.install(
        database_queue_registration(
            database_factory=lambda: SQLiteDriver(queue_path),
            storage_identity=storage_identity,
        )
    )
    try:
        return manager.binding()
    finally:
        manager.close()


def _profile(path: Path, binding: CapabilityBinding) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "driver_name": "database",
                "binding": asdict(binding),
                "qualification": {
                    "evidence_digest": "d" * 64,
                    "environment_digest": "e" * 64,
                    "observed_at": "2026-08-18T08:00:00.000000Z",
                    "expires_at": "2099-08-18T08:00:00.000000Z",
                    "sample_count": 10000,
                    "real_service": True,
                    "correctness_failures": 0,
                    "settlement_failures": 0,
                    "reservation_losses": 0,
                },
                "optimization": {
                    "worker_count": 2,
                    "max_concurrency": 3,
                    "poll_interval_ms": 25,
                    "heartbeat_interval_ms": 5000,
                    "max_idle_time_ms": 120000,
                    "queue_wait_alert_after_ms": 3000,
                    "renewal_min_interval_ms": 100,
                    "renewal_max_interval_ms": 10000,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def test_default_bootstrap_does_not_enable_worker_queue() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    try:
        with pytest.raises(ServiceNotBound):
            application.container.resolve(WorkerGenerationFactory)
    finally:
        application.shutdown()


def test_bootstrap_exposes_generation_factory_only_with_exact_bound_profile(
    tmp_path: Path,
) -> None:
    queue_path = tmp_path / "worker-queue.sqlite3"
    profile_path = tmp_path / "worker-profile.json"
    _profile(profile_path, _binding(queue_path))
    application = create_application(
        settings=Settings(
            worker_queue_sqlite_path=str(queue_path),
            worker_optimization_profile_path=str(profile_path),
        ),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    try:
        assert application.program_jobs is not None
        assert application.program_job_handler is not None
        job_id = application.program_jobs.submit(
            ProgramRequest(
                argv=("python", "-c", "print('queued')"),
                working_directory=tmp_path,
                budget=ProgramBudget(timeout=timedelta(seconds=3), max_output_bytes=256),
            )
        )
        assert [event.state.value for event in application.program_jobs.events_after(job_id)] == [
            "queued"
        ]
        factory = application.container.resolve(WorkerGenerationFactory)
        runtime = factory.create(application.program_job_handler, lane="agent")
        assert runtime.worker_count == 2
        assert runtime.max_concurrency == 3
        assert runtime.poll_interval == timedelta(milliseconds=25)
        assert runtime.lane == "priority"

        async def run_worker() -> None:
            await runtime.start()
            try:
                for _ in range(100):
                    result = application.program_jobs.result(job_id)
                    if result is not None:
                        assert result.result.stdout == b"queued\n"
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("configured worker did not execute the program job")
            finally:
                await runtime.close()

        asyncio.run(run_worker())
        assert queue_path.is_file()
    finally:
        application.shutdown()


def test_bootstrap_rejects_profile_binding_drift_before_opening_queue(
    tmp_path: Path,
) -> None:
    queue_path = tmp_path / "worker-queue.sqlite3"
    profile_path = tmp_path / "worker-profile.json"
    binding = _binding(queue_path)
    _profile(
        profile_path,
        CapabilityBinding(
            driver_version=binding.driver_version,
            artifact_digest=binding.artifact_digest,
            dependency_graph_digest=binding.dependency_graph_digest,
            configuration_digest="f" * 64,
            deployment_profile=binding.deployment_profile,
            policy_version=binding.policy_version,
        ),
    )

    with pytest.raises(ApplicationBootError):
        create_application(
            settings=Settings(
                worker_queue_sqlite_path=str(queue_path),
                worker_optimization_profile_path=str(profile_path),
            ),
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
        )

    assert not queue_path.exists()


def test_bootstrap_rejects_queue_storage_drift_under_same_profile(tmp_path: Path) -> None:
    qualified_queue = tmp_path / "qualified-worker-queue.sqlite3"
    changed_queue = tmp_path / "changed-worker-queue.sqlite3"
    profile_path = tmp_path / "worker-profile.json"
    _profile(profile_path, _binding(qualified_queue))

    with pytest.raises(ApplicationBootError):
        create_application(
            settings=Settings(
                worker_queue_sqlite_path=str(changed_queue),
                worker_optimization_profile_path=str(profile_path),
            ),
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
        )

    assert not qualified_queue.exists()
    assert not changed_queue.exists()


@pytest.mark.parametrize(
    "document",
    [
        b"not-json",
        json.dumps({"schema_version": 1, "driver_name": "database"}).encode(),
        b"{}" * 40_000,
    ],
)
def test_bootstrap_rejects_malformed_or_unbounded_worker_profile(
    tmp_path: Path,
    document: bytes,
) -> None:
    queue_path = tmp_path / "worker-queue.sqlite3"
    profile_path = tmp_path / "worker-profile.json"
    profile_path.write_bytes(document)

    with pytest.raises(ApplicationBootError):
        create_application(
            settings=Settings(
                worker_queue_sqlite_path=str(queue_path),
                worker_optimization_profile_path=str(profile_path),
            ),
            task_repository=InMemoryTaskRepository(),
            trace_repository=InMemoryTraceRepository(),
        )

    assert not queue_path.exists()
