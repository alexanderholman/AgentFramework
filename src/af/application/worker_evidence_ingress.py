"""Authenticated raw-measurement ingress for worker optimization candidates."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import islice
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from af.application.bus import AuthorizationRequest, Gate, GateEffect
from af.application.worker_qualification import (
    WorkerOptimizationCandidate,
    WorkerOptimizationQualification,
)
from af.framework.context import AcceptanceScope, ExecutionContext
from af.framework.errors import InvalidProviderConfiguration, PlatformError, PlatformReason

__all__ = (
    "HmacWorkerMeasurementAuthenticator",
    "WorkerOptimizationMeasurement",
    "WorkerOptimizationMeasurementAuthenticator",
    "WorkerOptimizationMeasurementIngress",
    "WorkerOptimizationMeasurementReplayLedger",
    "canonical_worker_measurement_payload",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_INDEX = 1_000_000
_MAX_MEASUREMENTS = 256


def _positive_count(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"worker optimization measurement {field} must be positive")
    return value


def _non_negative_count(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"worker optimization measurement {field} must be non-negative")
    return value


def _duration_microseconds(value: timedelta | None) -> int | None:
    if value is None:
        return None
    return (value.days * 86_400 + value.seconds) * 1_000_000 + value.microseconds


@dataclass(frozen=True, slots=True, repr=False)
class WorkerOptimizationMeasurement:
    """One signed raw real-service observation for an exact worker profile."""

    issuer: str
    qualification: WorkerOptimizationQualification
    completed_jobs: int
    elapsed_microseconds: int
    total_cost_microunits: int
    signature: str

    def __post_init__(self) -> None:
        if type(self.issuer) is not str or _IDENTIFIER.fullmatch(self.issuer) is None:
            raise ValueError("worker optimization measurement issuer must be canonical")
        if not isinstance(self.qualification, WorkerOptimizationQualification):
            raise TypeError("worker optimization measurement qualification is invalid")
        _positive_count(self.completed_jobs, "completed jobs")
        _positive_count(self.elapsed_microseconds, "elapsed microseconds")
        _non_negative_count(self.total_cost_microunits, "total cost")
        if type(self.signature) is not str or _DIGEST.fullmatch(self.signature) is None:
            raise ValueError("worker optimization measurement signature must be lowercase SHA-256")

    def __repr__(self) -> str:
        return (
            "WorkerOptimizationMeasurement(issuer='[REDACTED]', "
            "qualification='[REDACTED]', measurements='[REDACTED]', "
            "signature='[REDACTED]')"
        )


def canonical_worker_measurement_payload(measurement: WorkerOptimizationMeasurement) -> bytes:
    """Return the canonical bytes authenticated by a trusted qualification runner.

    The evidence digest and signature are deliberately excluded: the digest is derived from these
    bytes and the signature authenticates them.
    """

    if not isinstance(measurement, WorkerOptimizationMeasurement):
        raise TypeError("worker optimization measurement is invalid")
    qualification = measurement.qualification
    optimization = qualification.optimization
    payload = {
        "binding": asdict(qualification.binding),
        "completed_jobs": measurement.completed_jobs,
        "correctness_failures": qualification.correctness_failures,
        "driver_name": qualification.driver_name,
        "elapsed_microseconds": measurement.elapsed_microseconds,
        "environment_digest": qualification.environment_digest,
        "expires_at": qualification.expires_at.isoformat(),
        "issuer": measurement.issuer,
        "observed_at": qualification.observed_at.isoformat(),
        "optimization": {
            "heartbeat_interval_us": _duration_microseconds(optimization.heartbeat_interval),
            "max_concurrency": optimization.max_concurrency,
            "max_idle_time_us": _duration_microseconds(optimization.max_idle_time),
            "poll_interval_us": _duration_microseconds(optimization.poll_interval),
            "queue_wait_alert_after_us": _duration_microseconds(
                optimization.queue_wait_alert_after
            ),
            "renewal_max_interval_us": _duration_microseconds(optimization.renewal_max_interval),
            "renewal_min_interval_us": _duration_microseconds(optimization.renewal_min_interval),
            "worker_count": optimization.worker_count,
        },
        "real_service": qualification.real_service,
        "reservation_losses": qualification.reservation_losses,
        "sample_count": qualification.sample_count,
        "settlement_failures": qualification.settlement_failures,
        "total_cost_microunits": measurement.total_cost_microunits,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


@runtime_checkable
class WorkerOptimizationMeasurementAuthenticator(Protocol):
    """Authenticate one measurement using a configured trusted producer identity."""

    def authenticate(self, measurement: WorkerOptimizationMeasurement) -> object: ...


class WorkerOptimizationMeasurementReplayLedger(Protocol):
    """Atomically consume signed evidence identities until their expiry."""

    def consume_batch(
        self,
        entries: tuple[tuple[str, str, datetime], ...],
        *,
        now: datetime,
    ) -> bool: ...


class HmacWorkerMeasurementAuthenticator:
    """Authenticate canonical measurement documents with per-issuer HMAC-SHA256 keys."""

    def __init__(self, issuer_keys: Mapping[str, bytes]) -> None:
        if not isinstance(issuer_keys, Mapping) or not issuer_keys:
            raise ValueError("worker measurement authentication keys must be non-empty")
        copied: dict[str, bytes] = {}
        for issuer, key in issuer_keys.items():
            if type(issuer) is not str or _IDENTIFIER.fullmatch(issuer) is None:
                raise ValueError("worker measurement authentication issuer is invalid")
            if type(key) is not bytes or len(key) < 32:
                raise ValueError("worker measurement authentication keys must be at least 32 bytes")
            copied[issuer] = bytes(key)
        self._issuer_keys = MappingProxyType(copied)

    def authenticate(self, measurement: WorkerOptimizationMeasurement) -> None:
        if not isinstance(measurement, WorkerOptimizationMeasurement):
            raise TypeError("worker optimization measurement is invalid")
        key = self._issuer_keys.get(measurement.issuer)
        if key is None:
            raise InvalidProviderConfiguration("worker measurement authentication failed")
        payload = canonical_worker_measurement_payload(measurement)
        expected_signature = hmac.new(key, payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(measurement.signature, expected_signature):
            raise InvalidProviderConfiguration("worker measurement authentication failed")
        expected_digest = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(measurement.qualification.evidence_digest, expected_digest):
            raise InvalidProviderConfiguration("worker measurement authentication failed")


def _validate_target(driver_name: object, environment_digest: object) -> tuple[str, str]:
    if type(driver_name) is not str or _IDENTIFIER.fullmatch(driver_name) is None:
        raise InvalidProviderConfiguration("worker measurement ingress driver is invalid")
    if type(environment_digest) is not str or _DIGEST.fullmatch(environment_digest) is None:
        raise InvalidProviderConfiguration("worker measurement ingress environment is invalid")
    return driver_name, environment_digest


def _current_utc(clock: Callable[[], datetime]) -> datetime:
    current = clock()
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise InvalidProviderConfiguration("worker measurement ingress time is invalid")
    return current.astimezone(UTC)


def _require_safe_evidence(qualification: WorkerOptimizationQualification, now: datetime) -> None:
    if qualification.observed_at > now:
        raise InvalidProviderConfiguration("worker measurement evidence is future-dated")
    if qualification.expires_at <= now:
        raise InvalidProviderConfiguration("worker measurement evidence is expired")
    if not qualification.real_service:
        raise InvalidProviderConfiguration("worker measurement evidence is not real-service")
    if (
        qualification.correctness_failures
        or qualification.settlement_failures
        or qualification.reservation_losses
    ):
        raise InvalidProviderConfiguration("worker measurement evidence has safety findings")


def _require_authentication_result(result: object) -> None:
    if inspect.iscoroutine(result):
        result.close()
        raise TypeError("worker measurement authenticator returned an asynchronous result")
    if inspect.isawaitable(result) or inspect.isasyncgen(result):
        raise TypeError("worker measurement authenticator returned an asynchronous result")
    if result is not None:
        raise TypeError("worker measurement authenticator must return None")


def _ratio_indices(values: tuple[Fraction, ...]) -> tuple[int, ...]:
    best = max(values)
    return tuple(
        value.numerator * best.denominator * _MAX_INDEX // (value.denominator * best.numerator)
        for value in values
    )


def _cost_indices(measurements: tuple[WorkerOptimizationMeasurement, ...]) -> tuple[int, ...]:
    if any(measurement.total_cost_microunits == 0 for measurement in measurements):
        return tuple(
            _MAX_INDEX if measurement.total_cost_microunits == 0 else 0
            for measurement in measurements
        )
    return _ratio_indices(
        tuple(
            Fraction(measurement.completed_jobs, measurement.total_cost_microunits)
            for measurement in measurements
        )
    )


class WorkerOptimizationMeasurementIngress:
    """Authorize, authenticate, bind, and normalize raw worker measurements."""

    def __init__(
        self,
        authenticator: WorkerOptimizationMeasurementAuthenticator,
        *,
        gate: Gate,
        replay_ledger: WorkerOptimizationMeasurementReplayLedger,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(authenticator, WorkerOptimizationMeasurementAuthenticator):
            raise TypeError("worker measurement authenticator is invalid")
        if not isinstance(gate, Gate):
            raise TypeError("worker measurement ingress gate is invalid")
        if not isinstance(replay_ledger, WorkerOptimizationMeasurementReplayLedger):
            raise TypeError("worker measurement replay ledger is invalid")
        if not callable(clock):
            raise TypeError("worker measurement ingress clock must be callable")
        self._authenticator = authenticator
        self._gate = gate
        self._replay_ledger = replay_ledger
        self._clock = clock

    def authenticate_and_normalize(
        self,
        measurements: Iterable[WorkerOptimizationMeasurement],
        *,
        context: ExecutionContext,
        driver_name: str,
        environment_digest: str,
    ) -> tuple[WorkerOptimizationCandidate, ...]:
        """Return normalized candidates only after exact-target authorization and authentication."""

        target_driver, target_environment = _validate_target(driver_name, environment_digest)
        decision = self._gate.decide(
            AuthorizationRequest(
                action="ingest",
                resource_type="worker-optimization-evidence",
                protected_resource_id=f"{target_driver}:{target_environment}",
                required_scope=AcceptanceScope.OPERATOR_EVALUATION,
            ),
            context,
        )
        if decision.effect is GateEffect.DENY:
            raise PlatformError(
                PlatformReason.AUTHORIZATION_DENIED,
                stage="worker.optimization.evidence.authorization",
                protected_cause=PermissionError("worker measurement ingress denied"),
            )

        try:
            available = tuple(islice(iter(measurements), _MAX_MEASUREMENTS + 1))
        except TypeError as error:
            raise TypeError("worker optimization measurements are invalid") from error
        if not 1 <= len(available) <= _MAX_MEASUREMENTS:
            raise InvalidProviderConfiguration(
                "worker measurement ingress requires a bounded non-empty measurement set"
            )
        current = _current_utc(self._clock)
        evidence_digests: set[str] = set()
        for measurement in available:
            if not isinstance(measurement, WorkerOptimizationMeasurement):
                raise TypeError("worker optimization measurements are invalid")
            authentication_result = self._authenticator.authenticate(measurement)
            _require_authentication_result(authentication_result)
            qualification = measurement.qualification
            if (
                qualification.driver_name != target_driver
                or qualification.environment_digest != target_environment
            ):
                raise InvalidProviderConfiguration(
                    "worker measurement does not match the authorized target"
                )
            _require_safe_evidence(qualification, current)
            if qualification.evidence_digest in evidence_digests:
                raise InvalidProviderConfiguration("worker measurement evidence must be unique")
            evidence_digests.add(qualification.evidence_digest)

        replay_entries = tuple(
            (
                measurement.issuer,
                measurement.qualification.evidence_digest,
                measurement.qualification.expires_at,
            )
            for measurement in available
        )
        if not self._replay_ledger.consume_batch(replay_entries, now=current):
            raise InvalidProviderConfiguration("worker measurement evidence was replayed")

        performance = _ratio_indices(
            tuple(
                Fraction(measurement.completed_jobs, measurement.elapsed_microseconds)
                for measurement in available
            )
        )
        safety = _ratio_indices(
            tuple(Fraction(measurement.qualification.sample_count) for measurement in available)
        )
        cost = _cost_indices(available)
        return tuple(
            WorkerOptimizationCandidate(
                measurement.qualification,
                performance[index],
                safety[index],
                cost[index],
            )
            for index, measurement in enumerate(available)
        )
