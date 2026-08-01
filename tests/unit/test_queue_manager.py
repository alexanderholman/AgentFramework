from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from af.framework.application import Application
from af.framework.container import Container, ProviderRegistrar, ServiceProvider
from af.framework.errors import PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    CapabilityValidationError,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    DriverResolutionError,
    HealthStatus,
    ManagerConfigurationError,
)
from af.framework.unit_of_work import UnitOfWork
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue.contracts import (
    QueueDispatchDisposition,
    QueueDispatchReceipt,
    QueueJob,
    QueueOperationError,
    QueueReservation,
    UnsupportedQueueFeature,
)
from af.queue.manager import QueueFeature, QueueManager, bind_queue_manager
from af.queue.sync import SyncQueueDriver
from af.queue.sync_manager import sync_queue_registration

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


def _job(message_id: str, **overrides: object) -> QueueJob:
    fields: dict[str, Any] = {
        "envelope": MessageEnvelope.create(
            metadata=MessageMetadata(
                message_id=message_id,
                message_type="jobs.example",
                schema_version=1,
                kind=EnvelopeKind.JOB,
                occurred_at=_NOW,
            ),
            payload={"message_id": message_id, "secret": "not-for-repr"},
        )
    }
    fields.update(overrides)
    return QueueJob(**fields)


def _manager(dispatched: list[str]) -> QueueManager:
    manager = QueueManager(
        connections={"primary": {"driver": "sync"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(
        sync_queue_registration(
            consumer=lambda job: dispatched.append(job.envelope.metadata.message_id),
            clock=lambda: _NOW,
        )
    )
    return manager


def test_queue_manager_dispatches_sync_jobs_inline() -> None:
    dispatched: list[str] = []
    manager = _manager(dispatched)

    receipt = manager.dispatch(_job("job-inline"))

    assert dispatched == ["job-inline"]
    assert receipt.message_id == "job-inline"
    assert receipt.lane == "default"
    assert receipt.disposition is QueueDispatchDisposition.INLINE
    assert receipt.dispatched_at == _NOW
    assert manager.connection(requires=(QueueFeature.INLINE_EXECUTION,)) is manager.connection()

    manager.close()

    with pytest.raises(DriverResolutionError):
        manager.dispatch(_job("job-closed"))


@pytest.mark.parametrize(
    ("job", "feature"),
    [
        (_job("job-delay", delay=timedelta(seconds=1)), QueueFeature.DELAY),
        (_job("job-priority", priority=10), QueueFeature.PRIORITY),
        (_job("job-retry", max_attempts=2), QueueFeature.RETRY),
        (_job("job-backoff", backoff=timedelta(seconds=5)), QueueFeature.RETRY),
        (_job("job-timeout", timeout=timedelta(seconds=30)), QueueFeature.TIMEOUT),
        (_job("job-unique", unique_key="job-unique"), QueueFeature.UNIQUENESS),
    ],
)
def test_queue_manager_fails_closed_for_unverified_job_features(
    job: QueueJob,
    feature: QueueFeature,
) -> None:
    manager = _manager([])

    with pytest.raises(CapabilityValidationError) as captured:
        manager.dispatch(job)

    assert captured.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    assert manager.resolved_connections == ()
    with pytest.raises(CapabilityValidationError) as reserve_error:
        manager.reserve()
    assert reserve_error.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    manager.close()


def test_queue_manager_dispatch_after_commit_runs_only_after_successful_commit() -> None:
    dispatched: list[str] = []
    manager = _manager(dispatched)
    driver = SQLiteDriver(":memory:")

    with UnitOfWork(driver) as unit:
        manager.dispatch_after_commit(unit, _job("job-commit"))
        assert dispatched == []

    assert dispatched == ["job-commit"]

    with pytest.raises(RuntimeError, match="rollback"), UnitOfWork(driver) as unit:
        manager.dispatch_after_commit(unit, _job("job-rollback"))
        raise RuntimeError("rollback")

    assert dispatched == ["job-commit"]
    manager.close()
    driver.close()


def test_queue_manager_rejects_untargeted_registration() -> None:
    manager = _manager([])
    registration = sync_queue_registration(consumer=lambda _job: None, clock=lambda: _NOW)
    untargeted = DriverRegistration(
        manager_id="database",
        driver_name=registration.driver_name,
        factory=registration.factory,
        manifest=registration.manifest,
        verifier=registration.verifier,
        disposer=registration.disposer,
    )

    with pytest.raises(ManagerConfigurationError):
        manager.install(untargeted)

    manager.close()


def test_queue_manager_is_application_owned_and_snapshots_configuration() -> None:
    dispatched: list[str] = []
    connections = {"primary": {"driver": "sync"}}

    class QueueProvider(ServiceProvider):
        provider_id = "queue"

        def register(self, registrar: ProviderRegistrar) -> None:
            bind_queue_manager(
                registrar,
                connections=connections,
                default="primary",
                deployment_profile="test",
                policy_version="policy-v1",
                registrations=(
                    sync_queue_registration(
                        consumer=lambda job: dispatched.append(job.envelope.metadata.message_id),
                        clock=lambda: _NOW,
                    ),
                ),
                clock=lambda: _NOW,
            )

    application = Application(Container(), (QueueProvider(),))
    application.register()
    connections["primary"]["driver"] = "broken"
    application.boot()
    application.serve()
    manager = application.container.resolve(QueueManager)

    manager.dispatch(_job("job-owned"))
    assert dispatched == ["job-owned"]

    application.shutdown()

    with pytest.raises(DriverResolutionError) as captured:
        manager.dispatch(_job("job-after-shutdown"))
    assert captured.value.reason is PlatformReason.DEPENDENCY_RESOLUTION_FAILED


def test_queue_provider_rejects_missing_or_duplicate_registrations() -> None:
    class Registrar:
        def bind(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del args, kwargs

    registration = sync_queue_registration(consumer=lambda _job: None, clock=lambda: _NOW)
    with pytest.raises(ManagerConfigurationError):
        bind_queue_manager(
            cast(ProviderRegistrar, Registrar()),
            connections={"primary": {"driver": "sync"}},
            default="primary",
            deployment_profile="test",
            policy_version="policy-v1",
            registrations=(),
        )
    with pytest.raises(ManagerConfigurationError):
        bind_queue_manager(
            cast(ProviderRegistrar, Registrar()),
            connections={"primary": {"driver": "sync"}},
            default="primary",
            deployment_profile="test",
            policy_version="policy-v1",
            registrations=(registration, registration),
        )


@pytest.mark.parametrize(
    "connection",
    [
        {"driver": "sync", "extra": True},
        {"driver": "other"},
    ],
)
def test_sync_registration_rejects_invalid_configuration(connection: dict[str, object]) -> None:
    manager = QueueManager(
        connections={"primary": connection},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(sync_queue_registration(consumer=lambda _job: None, clock=lambda: _NOW))

    with pytest.raises(DriverResolutionError):
        manager.connection()
    manager.close()


def test_queue_manager_rejects_connections_without_a_driver() -> None:
    with pytest.raises(ManagerConfigurationError):
        QueueManager(
            connections={"primary": {}},
            default="primary",
            deployment_profile="test",
            policy_version="policy-v1",
            clock=lambda: _NOW,
        )


def test_sync_registration_rejects_invalid_runtime_evidence_time() -> None:
    manager = QueueManager(
        connections={"primary": {"driver": "sync"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(
        sync_queue_registration(
            consumer=lambda _job: None,
            clock=lambda: datetime(2026, 8, 1, 12),
        )
    )

    with pytest.raises(CapabilityValidationError):
        manager.connection()
    manager.close()


def test_sync_queue_registration_rejects_invalid_consumer_identity_and_verifier_inputs() -> None:
    with pytest.raises(TypeError, match="consumer"):
        sync_queue_registration(consumer="not callable")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="clock"):
        sync_queue_registration(consumer=lambda _job: None, clock="not callable")  # type: ignore[arg-type]

    registration = sync_queue_registration(consumer=lambda _job: None, clock=lambda: _NOW)
    with pytest.raises(ValueError, match="invalid identity"):
        registration.factory(
            ConnectionRef("queue", "primary", "other"),
            {"driver": "sync"},
        )
    driver = registration.factory(ConnectionRef("queue", "primary", "sync"), {"driver": "sync"})
    binding = CapabilityBinding(
        driver_version=registration.manifest.version,
        artifact_digest=registration.manifest.artifact_digest,
        dependency_graph_digest=registration.manifest.dependency_graph_digest,
        configuration_digest="a" * 64,
        deployment_profile="test",
        policy_version="policy-v1",
    )
    with pytest.raises(ValueError, match="runtime capability verification failed"):
        registration.verifier(ConnectionRef("queue", "primary", "other"), driver, binding)


def test_queue_contracts_validate_and_redact_payload_content() -> None:
    job = _job(
        "job-contracts",
        lane="priority.high",
        delay=timedelta(seconds=1),
        priority=10,
        max_attempts=2,
        backoff=timedelta(seconds=5),
        timeout=timedelta(seconds=30),
        unique_key="unique.job-contracts",
    )
    receipt = QueueDispatchReceipt(
        message_id="job-contracts",
        lane="priority.high",
        disposition=QueueDispatchDisposition.ENQUEUED,
        dispatched_at=_NOW,
    )
    reservation = QueueReservation(job=job, attempt=2, reserved_at=_NOW)

    assert job.delay == timedelta(seconds=1)
    assert job.priority == 10
    assert job.max_attempts == 2
    assert job.backoff == timedelta(seconds=5)
    assert job.timeout == timedelta(seconds=30)
    assert job.unique_key == "unique.job-contracts"
    assert "not-for-repr" not in repr(job)
    assert receipt.disposition is QueueDispatchDisposition.ENQUEUED
    assert reservation.attempt == 2
    assert "job-contracts" in repr(reservation)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"lane": "NOT VALID"}, "lane"),
        ({"delay": timedelta(0)}, "delay"),
        ({"priority": "high"}, "priority"),
        ({"max_attempts": 0}, "maximum attempts"),
        ({"backoff": timedelta(0)}, "backoff"),
        ({"timeout": timedelta(0)}, "timeout"),
        ({"unique_key": "NOT VALID"}, "unique key"),
    ],
)
def test_queue_job_rejects_invalid_optional_values(
    kwargs: dict[str, object],
    error: str,
) -> None:
    with pytest.raises(ValueError, match=error):
        _job("job-invalid", **kwargs)


def test_queue_contracts_reject_invalid_envelopes_and_timestamps() -> None:
    with pytest.raises(ValueError, match="job kind"):
        QueueJob(
            envelope=MessageEnvelope.create(
                metadata=MessageMetadata(
                    message_id="job-domain",
                    message_type="jobs.example",
                    schema_version=1,
                    kind=EnvelopeKind.DOMAIN_EVENT,
                    occurred_at=_NOW,
                ),
                payload={"secret": "not-for-repr"},
            )
        )
    with pytest.raises(ValueError, match="disposition"):
        QueueDispatchReceipt(
            message_id="job-bad-disposition",
            lane="default",
            disposition="inline",  # type: ignore[arg-type]
            dispatched_at=_NOW,
        )
    with pytest.raises(ValueError, match="dispatch time"):
        QueueDispatchReceipt(
            message_id="job-bad-time",
            lane="default",
            disposition=QueueDispatchDisposition.INLINE,
            dispatched_at=datetime(2026, 8, 1, 12),
        )
    with pytest.raises(ValueError, match="reservation job"):
        QueueReservation(job=object(), attempt=1, reserved_at=_NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reservation attempt"):
        QueueReservation(job=_job("job-bad-attempt"), attempt=0, reserved_at=_NOW)


def test_sync_driver_reports_supported_and_unsupported_features() -> None:
    dispatched: list[str] = []
    driver = SyncQueueDriver(
        lambda job: dispatched.append(job.envelope.metadata.message_id),
        clock=lambda: _NOW,
    )
    reservation = QueueReservation(job=_job("job-reserved"), attempt=1, reserved_at=_NOW)

    assert driver.features == frozenset({QueueFeature.INLINE_EXECUTION})
    assert driver.supports(QueueFeature.INLINE_EXECUTION)
    assert not driver.supports(QueueFeature.RETRY)
    driver.require(QueueFeature.INLINE_EXECUTION)
    with pytest.raises(UnsupportedQueueFeature) as unsupported:
        driver.require(QueueFeature.RETRY)
    assert unsupported.value.reason is PlatformReason.CAPABILITY_UNAVAILABLE
    with pytest.raises(TypeError, match="feature"):
        driver.supports("retry")  # type: ignore[arg-type]

    assert driver.dispatch(_job("job-direct")).disposition is QueueDispatchDisposition.INLINE
    assert dispatched == ["job-direct"]
    with pytest.raises(UnsupportedQueueFeature):
        driver.reserve(lane="default")
    with pytest.raises(UnsupportedQueueFeature):
        driver.ack(reservation)
    with pytest.raises(UnsupportedQueueFeature):
        driver.reject(reservation)
    with pytest.raises(UnsupportedQueueFeature):
        driver.retry(reservation, available_at=_NOW + timedelta(seconds=1))


def test_sync_driver_rejects_invalid_inputs_and_surfaces_consumer_failures() -> None:
    with pytest.raises(TypeError, match="consumer"):
        SyncQueueDriver("not callable", clock=lambda: _NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="clock"):
        SyncQueueDriver(lambda _job: None, clock="not callable")  # type: ignore[arg-type]

    typed = SyncQueueDriver(
        lambda _job: (_ for _ in ()).throw(
            QueueOperationError(reason=PlatformReason.CANCELLED, stage="queue.consumer")
        ),
        clock=lambda: _NOW,
    )
    with pytest.raises(QueueOperationError) as typed_error:
        typed.dispatch(_job("job-typed-error"))
    assert typed_error.value.reason is PlatformReason.CANCELLED

    untyped = SyncQueueDriver(
        lambda _job: (_ for _ in ()).throw(RuntimeError("secret consumer detail")),
        clock=lambda: _NOW,
    )
    with pytest.raises(QueueOperationError) as captured:
        untyped.dispatch(_job("job-untyped-error"))
    assert captured.value.reason is PlatformReason.PERMANENT_FAILURE
    assert captured.value.stage == "queue.dispatch"
    assert "secret consumer detail" not in str(captured.value)

    bad_clock = SyncQueueDriver(lambda _job: None, clock=lambda: datetime(2026, 8, 1, 12))
    with pytest.raises(ValueError, match="dispatch time"):
        bad_clock.dispatch(_job("job-bad-clock"))
    with pytest.raises(TypeError, match="QueueJob"):
        typed.dispatch(object())  # type: ignore[arg-type]

    typed.close()
    with pytest.raises(QueueOperationError) as shutdown:
        typed.dispatch(_job("job-after-close"))
    assert shutdown.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_queue_manager_routes_ack_reject_and_retry_to_driver() -> None:
    calls: list[tuple[str, object]] = []

    class Driver:
        def dispatch(self, job: QueueJob, /) -> QueueDispatchReceipt:
            calls.append(("dispatch", job.envelope.metadata.message_id))
            return QueueDispatchReceipt(
                message_id=job.envelope.metadata.message_id,
                lane=job.lane,
                disposition=QueueDispatchDisposition.ENQUEUED,
                dispatched_at=_NOW,
            )

        def reserve(self, *, lane: str | None = None) -> QueueReservation | None:
            calls.append(("reserve", lane))
            return QueueReservation(job=_job("job-reserved"), attempt=1, reserved_at=_NOW)

        def ack(self, reservation: QueueReservation, /) -> None:
            calls.append(("ack", reservation.job.envelope.metadata.message_id))

        def reject(self, reservation: QueueReservation, /) -> None:
            calls.append(("reject", reservation.job.envelope.metadata.message_id))

        def retry(
            self,
            reservation: QueueReservation,
            /,
            *,
            available_at: datetime | None = None,
        ) -> None:
            calls.append(("retry", available_at))

        def close(self) -> None:
            calls.append(("close", None))

    manifest = DriverManifest(
        driver_name="scripted",
        version="1.0.0",
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        capabilities=CapabilitySet(
            (
                QueueFeature.INLINE_EXECUTION.value,
                QueueFeature.RESERVATION.value,
                QueueFeature.RETRY.value,
            )
        ),
    )

    def verify(
        ref: ConnectionRef,
        _driver: Driver,
        binding: CapabilityBinding,
    ) -> CapabilityEvidence:
        assert ref.manager_id == "queue"
        assert ref.driver_name == "scripted"
        return CapabilityEvidence(
            binding=binding,
            capabilities=manifest.capabilities,
            health=HealthStatus.HEALTHY,
            observed_at=_NOW,
            expires_at=_NOW + timedelta(minutes=1),
            evidence_digest="c" * 64,
        )

    registration: DriverRegistration[Driver] = DriverRegistration(
        manager_id="queue",
        driver_name="scripted",
        factory=lambda _ref, _config: Driver(),
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )

    manager = QueueManager(
        connections={"primary": {"driver": "scripted"}},
        default="primary",
        deployment_profile="test",
        policy_version="policy-v1",
        clock=lambda: _NOW,
    )
    manager.install(cast(DriverRegistration[Any], registration))

    reservation = manager.reserve(lane="lane-a")
    assert reservation is not None
    manager.ack(reservation)
    manager.reject(reservation)
    manager.retry(reservation, available_at=_NOW + timedelta(seconds=1))
    assert calls[:4] == [
        ("reserve", "lane-a"),
        ("ack", "job-reserved"),
        ("reject", "job-reserved"),
        ("retry", _NOW + timedelta(seconds=1)),
    ]
    with pytest.raises(ValueError, match="capability"):
        manager.connection(requires=(object(),))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="QueueJob"):
        manager.dispatch(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="UnitOfWork"):
        manager.dispatch_after_commit(object(), _job("job-type"))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="QueueJob"):
        manager.dispatch_after_commit(UnitOfWork(SQLiteDriver(":memory:")), object())  # type: ignore[arg-type]
    manager.close()


def test_queue_manager_preserves_generic_capability_inputs() -> None:
    manager = _manager([])

    driver = manager.connection(requires=CapabilitySet((QueueFeature.INLINE_EXECUTION.value,)))
    assert manager.connection(requires=QueueFeature.INLINE_EXECUTION.value) is driver
    manager.close()
