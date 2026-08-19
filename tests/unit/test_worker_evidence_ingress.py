from __future__ import annotations

import hashlib
import hmac
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from af.application.bus import Gate, GateDecision, GateEffect
from af.application.worker_evidence_ingress import (
    HmacWorkerMeasurementAuthenticator,
    WorkerOptimizationMeasurement,
    WorkerOptimizationMeasurementIngress,
    canonical_worker_measurement_payload,
)
from af.application.worker_generation import WorkerRuntimeOptimization
from af.application.worker_qualification import WorkerOptimizationQualification
from af.framework.context import AcceptanceScope, ExecutionContext, Principal
from af.framework.errors import InvalidProviderConfiguration, PlatformError, PlatformReason
from af.framework.managers import CapabilityBinding

NOW = datetime(2026, 8, 19, tzinfo=UTC)
KEY = b"k" * 32


def _context() -> ExecutionContext:
    return ExecutionContext(
        principal=Principal("operator-1", "user", roles=("operator",)),
        requested_scope=AcceptanceScope.OPERATOR_EVALUATION,
    )


def _allowing_gate(requests: list[tuple[str, str, str | None]]) -> Gate:
    def allow(request, context):  # type: ignore[no-untyped-def]
        del context
        requests.append((request.action, request.resource_type, request.protected_resource_id))
        return GateDecision(
            GateEffect.ALLOW,
            "policy-v1",
            "operator_allowed",
            effective_scope=AcceptanceScope.OPERATOR_EVALUATION,
        )

    return Gate(policy_version="policy-v1", policies=(allow,))


def _qualification(*, samples: int = 100, environment: str = "e" * 64):
    return WorkerOptimizationQualification(
        driver_name="database",
        binding=CapabilityBinding(
            driver_version="1.0.0",
            artifact_digest="a" * 64,
            dependency_graph_digest="b" * 64,
            configuration_digest="c" * 64,
            deployment_profile="production",
            policy_version="policy-v1",
        ),
        optimization=WorkerRuntimeOptimization(
            worker_count=1,
            max_concurrency=2,
            poll_interval=timedelta(milliseconds=10),
            heartbeat_interval=timedelta(seconds=1),
            max_idle_time=None,
            queue_wait_alert_after=None,
            renewal_min_interval=timedelta(milliseconds=100),
            renewal_max_interval=timedelta(seconds=1),
        ),
        evidence_digest="0" * 64,
        environment_digest=environment,
        observed_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(hours=1),
        sample_count=samples,
        real_service=True,
        correctness_failures=0,
        settlement_failures=0,
        reservation_losses=0,
    )


def _measurement(
    *,
    completed: int = 100,
    elapsed_us: int = 1_000_000,
    cost_microunits: int = 1_000,
    samples: int = 100,
    key: bytes = KEY,
) -> WorkerOptimizationMeasurement:
    measurement = WorkerOptimizationMeasurement(
        issuer="qualification-runner",
        qualification=_qualification(samples=samples),
        completed_jobs=completed,
        elapsed_microseconds=elapsed_us,
        total_cost_microunits=cost_microunits,
        signature="0" * 64,
    )
    digest = hashlib.sha256(canonical_worker_measurement_payload(measurement)).hexdigest()
    measurement = replace(
        measurement,
        qualification=replace(measurement.qualification, evidence_digest=digest),
    )
    signature = hmac.new(
        key,
        canonical_worker_measurement_payload(measurement),
        hashlib.sha256,
    ).hexdigest()
    return replace(measurement, signature=signature)


def _service(
    requests: list[tuple[str, str, str | None]],
) -> WorkerOptimizationMeasurementIngress:
    return WorkerOptimizationMeasurementIngress(
        HmacWorkerMeasurementAuthenticator({"qualification-runner": KEY}),
        gate=_allowing_gate(requests),
        clock=lambda: NOW,
    )


def test_ingress_denies_before_measurement_iteration_or_authentication() -> None:
    class ExplodingMeasurements:
        def __iter__(self):  # type: ignore[no-untyped-def]
            raise AssertionError("measurements must remain private before authorization")

    service = WorkerOptimizationMeasurementIngress(
        HmacWorkerMeasurementAuthenticator({"qualification-runner": KEY}),
        gate=Gate(policy_version="policy-v1", policies=()),
        clock=lambda: NOW,
    )

    with pytest.raises(PlatformError) as denied:
        service.authenticate_and_normalize(
            ExplodingMeasurements(),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )

    assert denied.value.reason is PlatformReason.AUTHORIZATION_DENIED


def test_ingress_stops_reading_after_bounded_measurement_limit() -> None:
    consumed = 0

    def oversized_measurements():  # type: ignore[no-untyped-def]
        nonlocal consumed
        for _ in range(1_000):
            consumed += 1
            yield _measurement()

    with pytest.raises(InvalidProviderConfiguration, match="bounded non-empty"):
        _service([]).authenticate_and_normalize(
            oversized_measurements(),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )

    assert consumed == 257


def test_ingress_authenticates_and_derives_indices_from_exact_raw_units() -> None:
    requests: list[tuple[str, str, str | None]] = []
    service = _service(requests)
    candidates = service.authenticate_and_normalize(
        (
            _measurement(completed=100, elapsed_us=1_000_000, cost_microunits=1_000, samples=50),
            _measurement(completed=200, elapsed_us=1_000_000, cost_microunits=4_000, samples=100),
        ),
        context=_context(),
        driver_name="database",
        environment_digest="e" * 64,
    )

    assert [candidate.performance_index for candidate in candidates] == [500_000, 1_000_000]
    assert [candidate.safety_index for candidate in candidates] == [500_000, 1_000_000]
    assert [candidate.cost_efficiency_index for candidate in candidates] == [1_000_000, 500_000]
    assert requests == [("ingest", "worker-optimization-evidence", f"database:{'e' * 64}")]


@pytest.mark.parametrize("change", ["payload", "signature", "issuer"])
def test_ingress_rejects_tampered_or_untrusted_measurements(change: str) -> None:
    measurement = _measurement()
    if change == "payload":
        measurement = replace(measurement, completed_jobs=101)
    elif change == "signature":
        measurement = replace(measurement, signature="f" * 64)
    else:
        measurement = replace(measurement, issuer="unknown-runner")

    with pytest.raises(InvalidProviderConfiguration, match="authentication"):
        _service([]).authenticate_and_normalize(
            (measurement,),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )


def test_ingress_rejects_target_drift_after_authentication() -> None:
    measurement = _measurement()
    drifted = replace(
        measurement,
        qualification=replace(measurement.qualification, environment_digest="f" * 64),
    )
    digest = hashlib.sha256(canonical_worker_measurement_payload(drifted)).hexdigest()
    drifted = replace(drifted, qualification=replace(drifted.qualification, evidence_digest=digest))
    drifted = replace(
        drifted,
        signature=hmac.new(
            KEY, canonical_worker_measurement_payload(drifted), hashlib.sha256
        ).hexdigest(),
    )

    with pytest.raises(InvalidProviderConfiguration, match="authorized target"):
        _service([]).authenticate_and_normalize(
            (drifted,),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )


def test_ingress_rejects_stale_synthetic_or_failure_bearing_evidence() -> None:
    for changes in (
        {"expires_at": NOW},
        {"real_service": False},
        {"correctness_failures": 1},
    ):
        measurement = _measurement()
        qualification = replace(measurement.qualification, **changes)
        unsigned = replace(measurement, qualification=qualification)
        digest = hashlib.sha256(canonical_worker_measurement_payload(unsigned)).hexdigest()
        unsigned = replace(unsigned, qualification=replace(qualification, evidence_digest=digest))
        signed = replace(
            unsigned,
            signature=hmac.new(
                KEY, canonical_worker_measurement_payload(unsigned), hashlib.sha256
            ).hexdigest(),
        )
        with pytest.raises(InvalidProviderConfiguration):
            _service([]).authenticate_and_normalize(
                (signed,),
                context=_context(),
                driver_name="database",
                environment_digest="e" * 64,
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"completed_jobs": 0},
        {"elapsed_microseconds": 0},
        {"total_cost_microunits": -1},
        {"signature": "not-a-signature"},
    ],
)
def test_measurement_rejects_invalid_raw_boundaries(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "issuer": "qualification-runner",
        "qualification": _qualification(),
        "completed_jobs": 1,
        "elapsed_microseconds": 1,
        "total_cost_microunits": 0,
        "signature": "0" * 64,
    }
    values.update(changes)
    with pytest.raises((TypeError, ValueError)):
        WorkerOptimizationMeasurement(**values)  # type: ignore[arg-type]


def test_hmac_authenticator_rejects_weak_keys_and_defensively_copies_mapping() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        HmacWorkerMeasurementAuthenticator({})
    with pytest.raises(ValueError, match="at least 32 bytes"):
        HmacWorkerMeasurementAuthenticator({"qualification-runner": b"short"})

    keys = {"qualification-runner": KEY}
    authenticator = HmacWorkerMeasurementAuthenticator(keys)
    keys.clear()
    authenticator.authenticate(_measurement())


@pytest.mark.parametrize("kind", ["coroutine", "value"])
def test_ingress_rejects_asynchronous_or_value_returning_authenticators(kind: str) -> None:
    class InvalidAuthenticator:
        def authenticate(self, measurement: WorkerOptimizationMeasurement):  # type: ignore[no-untyped-def]
            del measurement
            if kind == "coroutine":
                return self._authenticate()
            return object()

        async def _authenticate(self) -> None:
            return None

    service = WorkerOptimizationMeasurementIngress(
        InvalidAuthenticator(),
        gate=_allowing_gate([]),
        clock=lambda: NOW,
    )

    with pytest.raises(TypeError, match="authenticator"):
        service.authenticate_and_normalize(
            (_measurement(),),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )


def test_ingress_rejects_evidence_digest_drift_and_duplicate_documents() -> None:
    measurement = _measurement()
    drifted = replace(
        measurement,
        qualification=replace(measurement.qualification, evidence_digest="f" * 64),
    )
    with pytest.raises(InvalidProviderConfiguration, match="authentication"):
        _service([]).authenticate_and_normalize(
            (drifted,),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )
    with pytest.raises(InvalidProviderConfiguration, match="unique"):
        _service([]).authenticate_and_normalize(
            (measurement, measurement),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )


def test_ingress_handles_authenticated_zero_cost_without_inventing_a_denominator() -> None:
    candidates = _service([]).authenticate_and_normalize(
        (
            _measurement(completed=1, cost_microunits=0),
            _measurement(completed=1, cost_microunits=1),
        ),
        context=_context(),
        driver_name="database",
        environment_digest="e" * 64,
    )
    assert [candidate.cost_efficiency_index for candidate in candidates] == [1_000_000, 0]


@pytest.mark.parametrize(
    "authenticator, gate, clock",
    [
        (object(), Gate(policy_version="policy-v1", policies=()), lambda: NOW),
        (HmacWorkerMeasurementAuthenticator({"qualification-runner": KEY}), object(), lambda: NOW),
        (
            HmacWorkerMeasurementAuthenticator({"qualification-runner": KEY}),
            Gate(policy_version="policy-v1", policies=()),
            object(),
        ),
    ],
)
def test_ingress_rejects_invalid_composition(
    authenticator: object,
    gate: object,
    clock: object,
) -> None:
    with pytest.raises(TypeError):
        WorkerOptimizationMeasurementIngress(
            authenticator,  # type: ignore[arg-type]
            gate=gate,  # type: ignore[arg-type]
            clock=clock,  # type: ignore[arg-type]
        )


def test_ingress_rejects_invalid_time_target_and_collection_boundaries() -> None:
    invalid_clock = WorkerOptimizationMeasurementIngress(
        HmacWorkerMeasurementAuthenticator({"qualification-runner": KEY}),
        gate=_allowing_gate([]),
        clock=lambda: datetime(2026, 8, 19),
    )
    with pytest.raises(InvalidProviderConfiguration, match="time"):
        invalid_clock.authenticate_and_normalize(
            (_measurement(),),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )
    with pytest.raises(InvalidProviderConfiguration, match="non-empty"):
        _service([]).authenticate_and_normalize(
            (),
            context=_context(),
            driver_name="database",
            environment_digest="e" * 64,
        )
    with pytest.raises(InvalidProviderConfiguration, match="driver"):
        _service([]).authenticate_and_normalize(
            (_measurement(),),
            context=_context(),
            driver_name="INVALID",
            environment_digest="e" * 64,
        )
    with pytest.raises(InvalidProviderConfiguration, match="environment"):
        _service([]).authenticate_and_normalize(
            (_measurement(),),
            context=_context(),
            driver_name="database",
            environment_digest="invalid",
        )
    with pytest.raises(TypeError, match="measurement"):
        canonical_worker_measurement_payload(object())  # type: ignore[arg-type]
