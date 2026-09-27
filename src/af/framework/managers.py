"""Deterministic manager, named-driver, capability, and composite contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from inspect import isasyncgen, isasyncgenfunction, isawaitable, iscoroutinefunction
from math import isfinite
from threading import RLock
from types import MappingProxyType
from typing import Protocol, cast, final

from af.framework.container import ProviderRegistrar, Resolver, Scope
from af.framework.errors import PlatformError, PlatformReason, ServiceNotBound
from af.observability.context import TelemetryContext
from af.observability.signals import (
    CapabilityState,
    DegradationState,
    HealthState,
    LivenessState,
    ReadinessState,
    TelemetryPublisher,
    TelemetrySignal,
)

__all__ = (
    "CapabilityBinding",
    "CapabilityEvidence",
    "CapabilitySet",
    "CapabilityValidationError",
    "CapabilityVerifier",
    "ConnectionQuarantinedError",
    "ConnectionRef",
    "DegradationEvidence",
    "DriverFactory",
    "DriverManifest",
    "DriverRegistration",
    "DriverResolutionError",
    "DriverTarget",
    "FailoverDriver",
    "FallbackDriver",
    "HealthStatus",
    "LoadBalancingDriver",
    "Manager",
    "ManagerConfigurationError",
    "bind_driver",
    "bind_manager",
)

_IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.+-]{0,126}[A-Za-z0-9])?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_MAX_CONFIG_DEPTH = 24
_MAX_CONFIG_ITEMS = 10_000


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical identifier")
    return value


def _digest(value: object, field: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _bounded_version(value: object, field: str) -> str:
    if type(value) is not str or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded version identifier")
    return value


def _aware_utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class ManagerConfigurationError(PlatformError):
    """Sanitized invalid manager or connection configuration."""

    def __init__(
        self,
        *,
        protected_cause: BaseException | None = None,
        cleanup_failures: tuple[Exception, ...] = (),
    ) -> None:
        self.cleanup_failures = cleanup_failures
        super().__init__(
            PlatformReason.CONFIGURATION_INVALID,
            stage="manager.configuration",
            protected_cause=protected_cause,
        )


class DriverResolutionError(PlatformError):
    """Sanitized named-driver resolution failure."""

    def __init__(
        self,
        *,
        protected_cause: BaseException | None = None,
        cleanup_failures: tuple[Exception, ...] = (),
    ) -> None:
        self.cleanup_failures = cleanup_failures
        super().__init__(
            PlatformReason.DEPENDENCY_RESOLUTION_FAILED,
            stage="manager.driver-resolution",
            protected_cause=protected_cause,
        )


class CapabilityValidationError(PlatformError):
    """Sanitized capability, health, or evidence failure."""

    def __init__(
        self,
        *,
        protected_cause: BaseException | None = None,
        cleanup_failures: tuple[Exception, ...] = (),
    ) -> None:
        self.cleanup_failures = cleanup_failures
        super().__init__(
            PlatformReason.CAPABILITY_UNAVAILABLE,
            stage="manager.capability-validation",
            protected_cause=protected_cause,
        )


class ConnectionQuarantinedError(CapabilityValidationError):
    """A cached connection changed materially and requires explicit operator release."""


class HealthStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    """Canonical immutable capability identifiers."""

    values: frozenset[str]

    def __init__(self, values: Iterable[str] = ()) -> None:
        if isinstance(values, str):
            values = (values,)
        try:
            normalized = frozenset(_identifier(value, "capability") for value in values)
        except (TypeError, ValueError) as error:
            raise ValueError("capabilities must contain canonical identifiers") from error
        object.__setattr__(self, "values", normalized)

    def __contains__(self, capability: object) -> bool:
        return capability in self.values

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self.values))

    def __len__(self) -> int:
        return len(self.values)

    def missing(self, required: CapabilitySet | Iterable[str]) -> CapabilitySet:
        expected = required if isinstance(required, CapabilitySet) else CapabilitySet(required)
        return CapabilitySet(expected.values - self.values)

    def supports(self, required: CapabilitySet | Iterable[str]) -> bool:
        return len(self.missing(required)) == 0


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    manager_id: str
    connection_name: str
    driver_name: str

    def __post_init__(self) -> None:
        _identifier(self.manager_id, "manager id")
        _identifier(self.connection_name, "connection name")
        _identifier(self.driver_name, "driver name")

    @property
    def name(self) -> str:
        return self.connection_name

    @property
    def driver(self) -> str:
        return self.driver_name


@dataclass(frozen=True, slots=True)
class DriverManifest:
    """Trusted static identity and upper bound for one driver implementation."""

    driver_name: str
    version: str
    artifact_digest: str
    dependency_graph_digest: str
    capabilities: CapabilitySet

    def __post_init__(self) -> None:
        _identifier(self.driver_name, "driver name")
        _bounded_version(self.version, "driver version")
        _digest(self.artifact_digest, "artifact digest")
        _digest(self.dependency_graph_digest, "dependency graph digest")
        if not isinstance(self.capabilities, CapabilitySet):
            raise ValueError("driver capabilities must be a CapabilitySet")


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    """Identity values to which runtime/conformance evidence is bound."""

    driver_version: str
    artifact_digest: str
    dependency_graph_digest: str
    configuration_digest: str
    deployment_profile: str
    policy_version: str

    def __post_init__(self) -> None:
        _bounded_version(self.driver_version, "driver version")
        _digest(self.artifact_digest, "artifact digest")
        _digest(self.dependency_graph_digest, "dependency graph digest")
        _digest(self.configuration_digest, "configuration digest")
        _identifier(self.deployment_profile, "deployment profile")
        _bounded_version(self.policy_version, "policy version")


@dataclass(frozen=True, slots=True)
class CapabilityEvidence:
    """Independently produced runtime and conformance evidence."""

    binding: CapabilityBinding
    capabilities: CapabilitySet
    health: HealthStatus
    observed_at: datetime
    expires_at: datetime
    evidence_digest: str
    revoked: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.binding, CapabilityBinding):
            raise ValueError("capability evidence binding is invalid")
        if not isinstance(self.capabilities, CapabilitySet):
            raise ValueError("evidence capabilities must be a CapabilitySet")
        if not isinstance(self.health, HealthStatus):
            raise ValueError("evidence health must be a HealthStatus")
        observed = _aware_utc(self.observed_at, "evidence observation")
        expires = _aware_utc(self.expires_at, "evidence expiry")
        if expires <= observed:
            raise ValueError("capability evidence expiry must follow observation")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "expires_at", expires)
        _digest(self.evidence_digest, "evidence digest")
        if type(self.revoked) is not bool:
            raise ValueError("evidence revoked must be boolean")

    @property
    def material_fingerprint(self) -> tuple[object, ...]:
        return (
            self.binding,
            self.capabilities,
            self.health,
            self.revoked,
        )


class DriverFactory[T_co](Protocol):
    """Provider-extensible constructor for a validated named connection."""

    def __call__(self, ref: ConnectionRef, config: Mapping[str, object], /) -> T_co: ...


class CapabilityVerifier[T_contra](Protocol):
    """Trusted verifier kept separate from the driver's self-report."""

    def __call__(
        self,
        ref: ConnectionRef,
        driver: T_contra,
        expected_binding: CapabilityBinding,
        /,
    ) -> CapabilityEvidence: ...


type DriverDisposer[T] = Callable[[T], object]


def _is_async_callable(value: object) -> bool:
    if iscoroutinefunction(value) or isasyncgenfunction(value):
        return True
    try:
        call = type(value).__call__
    except Exception as error:
        raise ValueError("callback contract cannot be inspected") from error
    return iscoroutinefunction(call) or isasyncgenfunction(call)


def _validate_sync_callable(value: object, field: str) -> None:
    if not callable(value) or _is_async_callable(value):
        raise ValueError(f"{field} must be a synchronous callable")


def _reject_async_result(value: object, field: str) -> None:
    if not isawaitable(value) and not isasyncgen(value):
        return
    try:
        close = getattr(value, "close", None)
        if callable(close):
            close()
        else:
            cancel = getattr(value, "cancel", None)
            if callable(cancel):
                cancel()
    except BaseException:
        pass
    raise TypeError(f"{field} returned an asynchronous result")


@final
@dataclass(frozen=True, slots=True, repr=False)
class DriverRegistration[T]:
    """Inert provider declaration for one manager-owned named driver."""

    manager_id: str
    driver_name: str
    factory: DriverFactory[T]
    manifest: DriverManifest
    verifier: CapabilityVerifier[T]
    disposer: Callable[[T], object] | None = None

    def __post_init__(self) -> None:
        manager_id = _identifier(self.manager_id, "manager id")
        driver_name = _identifier(self.driver_name, "driver name")
        _validate_sync_callable(self.factory, "driver factory")
        _validate_sync_callable(self.verifier, "capability verifier")
        if (
            not isinstance(self.manifest, DriverManifest)
            or self.manifest.driver_name != driver_name
        ):
            raise ValueError("driver manifest identity does not match registration")
        if self.disposer is not None:
            _validate_sync_callable(self.disposer, "driver disposer")
        object.__setattr__(self, "manager_id", manager_id)
        object.__setattr__(self, "driver_name", driver_name)

    def __repr__(self) -> str:
        return (
            "DriverRegistration("
            "manager_id='[REDACTED]', driver_name='[REDACTED]', "
            "factory='[REDACTED]', manifest='[REDACTED]', "
            "verifier='[REDACTED]', disposer='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True)
class _ConnectionDefinition:
    ref: ConnectionRef
    config: Mapping[str, object]
    configuration_digest: str


@dataclass(frozen=True, slots=True)
class _DriverRegistration[T]:
    factory: DriverFactory[T]
    manifest: DriverManifest
    verifier: CapabilityVerifier[T]
    disposer: DriverDisposer[T] | None


@dataclass(slots=True)
class _CachedDriver[T]:
    driver: T
    registration: _DriverRegistration[T]
    evidence: CapabilityEvidence


@dataclass(slots=True)
class _ConfigBudget:
    items: int = 0


def _snapshot_config(
    value: object,
    *,
    depth: int,
    budget: _ConfigBudget,
) -> object:
    if depth > _MAX_CONFIG_DEPTH:
        raise ValueError("connection configuration is too deeply nested")
    budget.items += 1
    if budget.items > _MAX_CONFIG_ITEMS:
        raise ValueError("connection configuration is too large")
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("connection configuration numbers must be finite")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or not key or len(key) > 200:
                raise ValueError("connection configuration keys must be bounded strings")
            frozen[key] = _snapshot_config(item, depth=depth + 1, budget=budget)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_snapshot_config(item, depth=depth + 1, budget=budget) for item in value)
    raise ValueError("connection configuration contains an unsupported value")


def _plain_config(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_config(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_config(item) for item in value]
    return value


def _config_digest(config: Mapping[str, object]) -> str:
    encoded = json.dumps(
        _plain_config(config),
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _driver_registration_qualifier(manager_id: str, driver_name: str) -> str:
    framed = bytearray()
    for value in (manager_id, driver_name):
        encoded = value.encode("utf-8")
        framed.extend(len(encoded).to_bytes(4, "big"))
        framed.extend(encoded)
    return f"af.manager-driver.{hashlib.sha256(framed).hexdigest()}"


def _snapshot_connections(connections: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(connections, Mapping):
        raise ValueError("connections must be a mapping")
    snapshot: dict[str, object] = {}
    for name, raw_config in connections.items():
        connection_name = _identifier(name, "connection name")
        if not isinstance(raw_config, Mapping):
            raise ValueError("connection must be a mapping")
        snapshot[connection_name] = _snapshot_config(
            raw_config,
            depth=0,
            budget=_ConfigBudget(),
        )
    return MappingProxyType(snapshot)


def _validated_driver_registration(value: object) -> DriverRegistration[object]:
    if type(value) is not DriverRegistration:
        cause = ValueError("driver registration is invalid")
        raise ManagerConfigurationError(protected_cause=cause) from cause
    registration = cast(DriverRegistration[object], value)
    try:
        registration.__post_init__()
    except (TypeError, ValueError) as error:
        raise ManagerConfigurationError(protected_cause=error) from error
    return registration


def bind_driver[T](
    registrar: ProviderRegistrar,
    registration: DriverRegistration[T],
) -> None:
    """Declare inert driver metadata under its deterministic manager/name key."""
    validated = _validated_driver_registration(registration)
    registrar.bind(
        DriverRegistration,
        lambda _resolver: validated,
        qualifier=_driver_registration_qualifier(
            validated.manager_id,
            validated.driver_name,
        ),
        scope=Scope.SINGLETON,
        owned=False,
        replace=False,
    )


def bind_manager(
    registrar: ProviderRegistrar,
    *,
    manager_id: str,
    connections: Mapping[str, object],
    default: str | None,
    deployment_profile: str,
    policy_version: str,
    clock: Callable[[], datetime] | None = None,
    telemetry: TelemetryPublisher | None = None,
) -> None:
    """Bind one application-owned manager assembled from inert driver declarations."""
    try:
        normalized_manager_id = _identifier(manager_id, "manager id")
        normalized_profile = _identifier(deployment_profile, "deployment profile")
        normalized_policy = _bounded_version(policy_version, "policy version")
        if default is not None:
            normalized_default: str | None = _identifier(default, "default connection")
        else:
            normalized_default = None
        if clock is not None:
            _validate_sync_callable(clock, "manager clock")
        connection_snapshot = _snapshot_connections(connections)
    except (TypeError, ValueError) as error:
        raise ManagerConfigurationError(protected_cause=error) from error

    def create_manager(resolver: Resolver) -> Manager[object]:
        manager: Manager[object] = Manager(
            manager_id=normalized_manager_id,
            connections=connection_snapshot,
            default=normalized_default,
            deployment_profile=normalized_profile,
            policy_version=normalized_policy,
            clock=clock,
            telemetry=telemetry,
        )
        driver_names = sorted({definition[0] for definition in manager._definitions.values()})
        for driver_name in driver_names:
            try:
                registration = resolver.resolve(
                    DriverRegistration,
                    qualifier=_driver_registration_qualifier(
                        normalized_manager_id,
                        driver_name,
                    ),
                )
            except ServiceNotBound as error:
                raise DriverResolutionError(protected_cause=error) from None
            if (
                type(registration) is not DriverRegistration
                or registration.manager_id != normalized_manager_id
                or registration.driver_name != driver_name
            ):
                cause = ValueError("driver registration identity does not match binding")
                raise ManagerConfigurationError(protected_cause=cause) from cause
            manager.extend(
                driver_name,
                registration.factory,
                manifest=registration.manifest,
                verifier=registration.verifier,
                disposer=registration.disposer,
            )
        return manager

    registrar.bind(
        Manager,
        create_manager,
        qualifier=normalized_manager_id,
        scope=Scope.APPLICATION,
        owned=True,
        replace=False,
    )


class Manager[T]:
    """Resolve configured named drivers lazily with verified, bounded caching."""

    def __init__(
        self,
        *,
        manager_id: str,
        connections: Mapping[str, object],
        default: str | None,
        deployment_profile: str,
        policy_version: str,
        clock: Callable[[], datetime] | None = None,
        telemetry: TelemetryPublisher | None = None,
    ) -> None:
        try:
            self._manager_id = _identifier(manager_id, "manager id")
            self._deployment_profile = _identifier(
                deployment_profile,
                "deployment profile",
            )
            self._policy_version = _bounded_version(policy_version, "policy version")
            if default is not None:
                _identifier(default, "default connection")
            if clock is not None:
                _validate_sync_callable(clock, "manager clock")
            if not isinstance(connections, Mapping):
                raise ValueError("connections must be a mapping")
            definitions: dict[str, tuple[str, Mapping[str, object], str]] = {}
            for name, raw_config in connections.items():
                connection_name = _identifier(name, "connection name")
                if not isinstance(raw_config, Mapping):
                    raise ValueError("connection must be a mapping")
                snapshot = _snapshot_config(
                    raw_config,
                    depth=0,
                    budget=_ConfigBudget(),
                )
                if not isinstance(snapshot, Mapping):
                    raise ValueError("connection must be a mapping")
                driver_name = _identifier(snapshot.get("driver"), "connection driver")
                definitions[connection_name] = (
                    driver_name,
                    snapshot,
                    _config_digest(snapshot),
                )
        except (TypeError, ValueError) as error:
            raise ManagerConfigurationError(protected_cause=error) from error

        self._default = default
        self._definitions = definitions
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._telemetry = telemetry
        self._registrations: dict[str, _DriverRegistration[T]] = {}
        self._cache: dict[str, _CachedDriver[T]] = {}
        self._owned_drivers: dict[int, T] = {}
        self._quarantined: set[str] = set()
        self._active: set[str] = set()
        self._resolution_order: list[str] = []
        self._callback_depth = 0
        self._closing = False
        self._closed = False
        self._lock = RLock()

    def _publish_driver_health(
        self,
        ref: ConnectionRef,
        evidence: CapabilityEvidence,
        transition: str,
    ) -> None:
        if self._telemetry is None:
            return
        try:
            observed_at = evidence.observed_at
            if transition == "quarantined":
                state = HealthState(
                    liveness=LivenessState.LIVE,
                    readiness=ReadinessState.NOT_READY,
                    degradation=DegradationState.QUARANTINED,
                    capability=CapabilityState.MISMATCH,
                    reason_code="driver.quarantined",
                    fresh_until=evidence.expires_at,
                )
            elif transition == "stopped":
                observed_at = _aware_utc(self._clock(), "manager clock")
                state = HealthState(
                    liveness=LivenessState.STOPPED,
                    readiness=ReadinessState.NOT_READY,
                    degradation=DegradationState.HEALTHY,
                    capability=CapabilityState.AVAILABLE,
                    reason_code="driver.stopped",
                    fresh_until=observed_at,
                )
            else:
                degraded = evidence.health is HealthStatus.DEGRADED
                state = HealthState(
                    liveness=LivenessState.LIVE,
                    readiness=ReadinessState.READY,
                    degradation=(
                        DegradationState.DEGRADED if degraded else DegradationState.HEALTHY
                    ),
                    capability=CapabilityState.AVAILABLE,
                    reason_code=("driver.degraded" if degraded else "driver.healthy"),
                    fresh_until=evidence.expires_at,
                )
            self._telemetry.publish(
                TelemetrySignal.health(
                    name="manager.driver-health",
                    observed_at=observed_at,
                    state=state,
                    context=TelemetryContext(operation="manager.driver-health"),
                    attributes={
                        "driver": ref.driver_name,
                        "manager": self._manager_id,
                        "transition": transition,
                    },
                )
            )
        except Exception:
            return

    @property
    def manager_id(self) -> str:
        return self._manager_id

    @property
    def resolved_connections(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._cache))

    @property
    def driver_manifests(self) -> tuple[DriverManifest, ...]:
        """Return inert registration declarations without resolving any connection."""
        with self._lock:
            return tuple(self._registrations[name].manifest for name in sorted(self._registrations))

    @property
    def quarantined_connections(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._quarantined))

    def evidence(self, name: str | None = None) -> CapabilityEvidence:
        """Return the last independently verified evidence for a resolved connection."""
        with self._lock:
            self._assert_operational()
            connection_name = self._requested_name(name)
            cached = self._cache.get(connection_name)
            if cached is None:
                raise DriverResolutionError(
                    protected_cause=LookupError("connection has no verified evidence")
                )
            return cached.evidence

    def reference(self, name: str | None = None) -> ConnectionRef:
        """Return the immutable configured identity for one named connection."""
        with self._lock:
            self._assert_operational()
            return self._definition(name).ref

    def health(self, name: str | None = None) -> HealthStatus:
        return self.evidence(name).health

    def extend(
        self,
        driver_name: str,
        factory: DriverFactory[T],
        *,
        manifest: DriverManifest,
        verifier: CapabilityVerifier[T],
        disposer: DriverDisposer[T] | None = None,
    ) -> None:
        try:
            normalized = _identifier(driver_name, "driver name")
            _validate_sync_callable(factory, "driver factory")
            _validate_sync_callable(verifier, "capability verifier")
            if not isinstance(manifest, DriverManifest) or manifest.driver_name != normalized:
                raise ValueError("driver manifest identity does not match registration")
            if disposer is not None:
                _validate_sync_callable(disposer, "driver disposer")
        except (TypeError, ValueError) as error:
            raise ManagerConfigurationError(protected_cause=error) from error

        with self._lock:
            if self._closing or self._closed or self._callback_depth:
                state_error = RuntimeError("manager does not accept driver extensions")
                raise ManagerConfigurationError(protected_cause=state_error) from state_error
            if normalized in self._registrations:
                duplicate_error = ValueError("driver factory is already registered")
                raise ManagerConfigurationError(
                    protected_cause=duplicate_error
                ) from duplicate_error
            self._registrations[normalized] = _DriverRegistration(
                factory,
                manifest,
                verifier,
                disposer,
            )

    def binding(self, name: str | None = None) -> CapabilityBinding:
        with self._lock:
            self._assert_operational()
            definition = self._definition(name)
            registration = self._registrations.get(definition.ref.driver_name)
            if registration is None:
                # The immutable expected binding is useful to an independent verifier before
                # the first resolution, but requires a registered manifest.
                raise DriverResolutionError(
                    protected_cause=LookupError("driver factory is not registered")
                )
            return self._binding(definition, registration.manifest)

    def connection(
        self,
        name: str | None = None,
        *,
        requires: CapabilitySet | Iterable[str] = (),
    ) -> T:
        try:
            required = requires if isinstance(requires, CapabilitySet) else CapabilitySet(requires)
        except (TypeError, ValueError) as error:
            raise CapabilityValidationError(protected_cause=error) from error

        with self._lock:
            self._assert_operational()
            definition = self._definition(name)
            connection_name = definition.ref.connection_name
            if connection_name in self._quarantined:
                raise ConnectionQuarantinedError(
                    protected_cause=RuntimeError("connection is quarantined")
                )
            if connection_name in self._active:
                raise DriverResolutionError(
                    protected_cause=RuntimeError("recursive driver resolution")
                )
            self._active.add(connection_name)
            try:
                cached = self._cache.get(connection_name)
                if cached is not None:
                    return self._reuse_cached(definition, cached, required)
                return self._construct(definition, required)
            finally:
                self._active.remove(connection_name)

    def purge(self, name: str | None = None) -> None:
        with self._lock:
            self._assert_operational()
            failures, interruption = self._purge_locked(name)
        if interruption is not None:
            raise interruption
        self._raise_disposal_failures(failures)

    def close(self) -> None:
        with self._lock:
            if self._closed or self._closing:
                return
            if self._callback_depth:
                raise DriverResolutionError(
                    protected_cause=RuntimeError("manager cannot close from a callback")
                )
            self._closing = True
            failures: list[Exception] = []
            interruption: BaseException | None = None
            try:
                failures, interruption = self._purge_locked(None)
            finally:
                self._registrations.clear()
                self._owned_drivers.clear()
                self._closing = False
                self._closed = True
        if interruption is not None:
            raise interruption
        self._raise_disposal_failures(failures)

    def _assert_operational(self) -> None:
        if self._closing or self._closed:
            raise DriverResolutionError(protected_cause=RuntimeError("manager is closed"))
        if self._callback_depth:
            raise DriverResolutionError(
                protected_cause=RuntimeError("manager callbacks cannot re-enter the manager")
            )

    def _purge_locked(
        self,
        name: str | None,
    ) -> tuple[list[Exception], BaseException | None]:
        names = (
            tuple(dict.fromkeys((*reversed(self._resolution_order), *self._quarantined)))
            if name is None
            else (self._requested_name(name),)
        )
        failures: list[Exception] = []
        interruption: BaseException | None = None
        for connection_name in names:
            cached = self._cache.pop(connection_name, None)
            self._quarantined.discard(connection_name)
            if connection_name in self._resolution_order:
                self._resolution_order.remove(connection_name)
            if cached is None:
                continue
            self._publish_driver_health(
                self._definition(connection_name).ref,
                cached.evidence,
                "stopped",
            )
            self._release_driver(cached.driver)
            try:
                self._dispose(cached)
            except BaseException as error:
                if isinstance(error, Exception):
                    failures.append(error)
                elif interruption is None:
                    interruption = error
        return failures, interruption

    @staticmethod
    def _raise_disposal_failures(failures: list[Exception]) -> None:
        if failures:
            raise DriverResolutionError(
                protected_cause=ExceptionGroup(
                    "driver disposal failed",
                    failures,
                ),
                cleanup_failures=tuple(failures),
            )

    def _requested_name(self, name: str | None) -> str:
        if name is None:
            if self._default is None:
                raise ManagerConfigurationError(
                    protected_cause=ValueError("default connection is not configured")
                )
            return self._default
        try:
            return _identifier(name, "connection name")
        except (TypeError, ValueError) as error:
            raise DriverResolutionError(protected_cause=error) from error

    def _definition(self, name: str | None) -> _ConnectionDefinition:
        requested = self._requested_name(name)
        raw = self._definitions.get(requested)
        if raw is None:
            raise DriverResolutionError(
                protected_cause=LookupError("named connection is not configured")
            )
        driver_name, config, digest = raw
        return _ConnectionDefinition(
            ConnectionRef(self._manager_id, requested, driver_name),
            config,
            digest,
        )

    def _binding(
        self,
        definition: _ConnectionDefinition,
        manifest: DriverManifest,
    ) -> CapabilityBinding:
        return CapabilityBinding(
            driver_version=manifest.version,
            artifact_digest=manifest.artifact_digest,
            dependency_graph_digest=manifest.dependency_graph_digest,
            configuration_digest=definition.configuration_digest,
            deployment_profile=self._deployment_profile,
            policy_version=self._policy_version,
        )

    def _construct(
        self,
        definition: _ConnectionDefinition,
        required: CapabilitySet,
    ) -> T:
        registration = self._registrations.get(definition.ref.driver_name)
        if registration is None:
            raise DriverResolutionError(
                protected_cause=LookupError("driver factory is not registered")
            )
        self._callback_depth += 1
        try:
            driver = registration.factory(definition.ref, definition.config)
            _reject_async_result(driver, "driver factory")
        except Exception as error:
            raise DriverResolutionError(protected_cause=error) from error
        finally:
            self._callback_depth -= 1
        if self._owned_drivers.get(id(driver)) is driver:
            raise DriverResolutionError(
                protected_cause=RuntimeError("driver instance is already manager-owned")
            )
        try:
            evidence = self._verify(definition, driver, registration)
        except BaseException as error:
            cleanup = self._try_dispose_driver(registration, driver)
            if cleanup is not None and isinstance(error, CapabilityValidationError):
                error.cleanup_failures = (cleanup,)
            raise
        cached = _CachedDriver(driver, registration, evidence)
        try:
            self._validate_evidence(
                cached.evidence,
                registration.manifest,
                self._binding(definition, registration.manifest),
                required,
            )
        except BaseException as error:
            cleanup = self._try_dispose(cached)
            if cleanup is not None and isinstance(error, CapabilityValidationError):
                error.cleanup_failures = (cleanup,)
            raise
        self._owned_drivers[id(driver)] = driver
        self._cache[definition.ref.connection_name] = cached
        self._resolution_order.append(definition.ref.connection_name)
        self._publish_driver_health(
            definition.ref,
            evidence,
            evidence.health.value,
        )
        return driver

    def _reuse_cached(
        self,
        definition: _ConnectionDefinition,
        cached: _CachedDriver[T],
        required: CapabilitySet,
    ) -> T:
        connection_name = definition.ref.connection_name
        try:
            current = self._verify(definition, cached.driver, cached.registration)
        except CapabilityValidationError as error:
            cleanup = self._quarantine(connection_name, cached)
            raise ConnectionQuarantinedError(
                protected_cause=error.protected_cause,
                cleanup_failures=(() if cleanup is None else (cleanup,)),
            ) from error
        if (
            self._cache.get(connection_name) is not cached
            or self._owned_drivers.get(id(cached.driver)) is not cached.driver
        ):
            self._quarantined.add(connection_name)
            raise ConnectionQuarantinedError(
                protected_cause=RuntimeError("cached driver changed during verification")
            )
        if current.material_fingerprint != cached.evidence.material_fingerprint:
            cleanup = self._quarantine(connection_name, cached)
            raise ConnectionQuarantinedError(
                protected_cause=RuntimeError("runtime capability evidence changed"),
                cleanup_failures=(() if cleanup is None else (cleanup,)),
            )
        try:
            self._validate_evidence(
                current,
                cached.registration.manifest,
                self._binding(definition, cached.registration.manifest),
                CapabilitySet(),
            )
        except CapabilityValidationError as error:
            cleanup = self._quarantine(connection_name, cached)
            raise ConnectionQuarantinedError(
                protected_cause=error.protected_cause,
                cleanup_failures=(() if cleanup is None else (cleanup,)),
            ) from error
        self._validate_evidence(
            current,
            cached.registration.manifest,
            self._binding(definition, cached.registration.manifest),
            required,
        )
        if (
            self._cache.get(connection_name) is not cached
            or self._owned_drivers.get(id(cached.driver)) is not cached.driver
        ):
            self._quarantined.add(connection_name)
            raise ConnectionQuarantinedError(
                protected_cause=RuntimeError("cached driver changed during validation")
            )
        previous_health = cached.evidence.health
        cached.evidence = current
        if current.health is not previous_health:
            self._publish_driver_health(
                definition.ref,
                current,
                current.health.value,
            )
        return cached.driver

    def _verify(
        self,
        definition: _ConnectionDefinition,
        driver: T,
        registration: _DriverRegistration[T],
    ) -> CapabilityEvidence:
        expected = self._binding(definition, registration.manifest)
        self._callback_depth += 1
        try:
            evidence = registration.verifier(definition.ref, driver, expected)
            _reject_async_result(evidence, "capability verifier")
        except Exception as error:
            raise CapabilityValidationError(protected_cause=error) from error
        finally:
            self._callback_depth -= 1
        if not isinstance(evidence, CapabilityEvidence):
            raise CapabilityValidationError(
                protected_cause=TypeError("verifier returned invalid evidence")
            )
        return evidence

    def _validate_evidence(
        self,
        evidence: CapabilityEvidence,
        manifest: DriverManifest,
        expected: CapabilityBinding,
        required: CapabilitySet,
    ) -> None:
        self._callback_depth += 1
        try:
            raw_now = self._clock()
            _reject_async_result(raw_now, "manager clock")
            now = _aware_utc(raw_now, "manager clock")
        except Exception as error:
            raise CapabilityValidationError(protected_cause=error) from error
        finally:
            self._callback_depth -= 1
        cause: BaseException | None = None
        if evidence.binding != expected:
            cause = ValueError("capability evidence identity does not match")
        elif evidence.observed_at > now:
            cause = ValueError("capability evidence is from the future")
        elif evidence.expires_at <= now:
            cause = ValueError("capability evidence is stale")
        elif evidence.revoked:
            cause = ValueError("capability evidence is revoked")
        elif evidence.health not in {HealthStatus.HEALTHY, HealthStatus.DEGRADED}:
            cause = RuntimeError("driver health is not eligible")
        elif not manifest.capabilities.supports(evidence.capabilities):
            cause = ValueError("runtime evidence exceeds trusted manifest")
        elif not evidence.capabilities.supports(required):
            cause = LookupError("required capability is unavailable")
        if cause is not None:
            raise CapabilityValidationError(protected_cause=cause)

    def _quarantine(self, name: str, cached: _CachedDriver[T]) -> Exception | None:
        self._publish_driver_health(
            self._definition(name).ref,
            cached.evidence,
            "quarantined",
        )
        if self._cache.get(name) is not cached:
            self._quarantined.add(name)
            return None
        self._cache.pop(name, None)
        if name in self._resolution_order:
            self._resolution_order.remove(name)
        self._quarantined.add(name)
        self._release_driver(cached.driver)
        return self._try_dispose(cached)

    def _release_driver(self, driver: T) -> None:
        if self._owned_drivers.get(id(driver)) is driver:
            self._owned_drivers.pop(id(driver), None)

    def _dispose(self, cached: _CachedDriver[T]) -> None:
        self._dispose_driver(cached.registration, cached.driver)

    def _dispose_driver(self, registration: _DriverRegistration[T], driver: T) -> None:
        if registration.disposer is not None:
            self._callback_depth += 1
            try:
                result = registration.disposer(driver)
                _reject_async_result(result, "driver disposer")
            finally:
                self._callback_depth -= 1

    def _try_dispose(self, cached: _CachedDriver[T]) -> Exception | None:
        return self._try_dispose_driver(cached.registration, cached.driver)

    def _try_dispose_driver(
        self,
        registration: _DriverRegistration[T],
        driver: T,
    ) -> Exception | None:
        try:
            self._dispose_driver(registration, driver)
        except Exception as error:
            return error
        return None


@dataclass(frozen=True, slots=True)
class DriverTarget[T]:
    ref: ConnectionRef
    driver: T

    def __post_init__(self) -> None:
        if not isinstance(self.ref, ConnectionRef):
            raise ValueError("driver target requires a ConnectionRef")


@dataclass(frozen=True, slots=True)
class DegradationEvidence:
    from_connection: ConnectionRef
    to_connection: ConnectionRef
    reason_code: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.from_connection, ConnectionRef) or not isinstance(
            self.to_connection,
            ConnectionRef,
        ):
            raise ValueError("degradation evidence requires connection references")
        _identifier(self.reason_code, "degradation reason")
        object.__setattr__(
            self,
            "occurred_at",
            _aware_utc(self.occurred_at, "degradation time"),
        )


class FallbackDriver[T]:
    """An explicit, observable switch from a primary to a designated fallback."""

    def __init__(
        self,
        primary: DriverTarget[T],
        fallback: DriverTarget[T],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(primary, DriverTarget) or not isinstance(fallback, DriverTarget):
            raise ValueError("fallback targets are invalid")
        if primary.ref == fallback.ref:
            raise ValueError("fallback must use a distinct connection")
        self._primary = primary
        self._fallback = fallback
        self._current = primary
        self._clock = clock or (lambda: datetime.now(UTC))
        self._evidence: list[DegradationEvidence] = []
        self._lock = RLock()

    @property
    def current(self) -> DriverTarget[T]:
        with self._lock:
            return self._current

    @property
    def evidence(self) -> tuple[DegradationEvidence, ...]:
        with self._lock:
            return tuple(self._evidence)

    def activate_fallback(self, reason_code: str) -> DriverTarget[T]:
        with self._lock:
            if self._current is self._fallback:
                return self._fallback
            evidence = DegradationEvidence(
                self._primary.ref,
                self._fallback.ref,
                reason_code,
                self._clock(),
            )
            self._evidence.append(evidence)
            self._current = self._fallback
            return self._current


class FailoverDriver[T]:
    """An ordered composite that changes target only through an explicit failover call."""

    def __init__(
        self,
        targets: tuple[DriverTarget[T], ...],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if len(targets) < 2 or any(not isinstance(target, DriverTarget) for target in targets):
            raise ValueError("failover requires at least two valid targets")
        if len({target.ref for target in targets}) != len(targets):
            raise ValueError("failover targets must be unique")
        self._targets = targets
        self._index = 0
        self._clock = clock or (lambda: datetime.now(UTC))
        self._evidence: list[DegradationEvidence] = []
        self._lock = RLock()

    @property
    def current(self) -> DriverTarget[T]:
        with self._lock:
            return self._targets[self._index]

    @property
    def evidence(self) -> tuple[DegradationEvidence, ...]:
        with self._lock:
            return tuple(self._evidence)

    def failover(self, reason_code: str) -> DriverTarget[T]:
        with self._lock:
            if self._index + 1 >= len(self._targets):
                raise DriverResolutionError(
                    protected_cause=LookupError("no failover target remains")
                )
            previous = self._targets[self._index]
            next_index = self._index + 1
            current = self._targets[next_index]
            evidence = DegradationEvidence(
                previous.ref,
                current.ref,
                reason_code,
                self._clock(),
            )
            self._evidence.append(evidence)
            self._index = next_index
            return current


class LoadBalancingDriver[T]:
    """A deterministic round-robin target selector with no implicit fallback semantics."""

    def __init__(self, targets: tuple[DriverTarget[T], ...]) -> None:
        if not targets or any(not isinstance(target, DriverTarget) for target in targets):
            raise ValueError("load balancing requires at least one valid target")
        if len({target.ref for target in targets}) != len(targets):
            raise ValueError("load balancing targets must be unique")
        self._targets = targets
        self._next_index = 0
        self._lock = RLock()

    @property
    def targets(self) -> tuple[DriverTarget[T], ...]:
        return self._targets

    def next(self) -> DriverTarget[T]:
        with self._lock:
            selected = self._targets[self._next_index]
            self._next_index = (self._next_index + 1) % len(self._targets)
            return selected
