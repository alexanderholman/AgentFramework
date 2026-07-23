"""Deterministic manager, named-driver, capability, and composite contracts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from threading import RLock
from types import MappingProxyType
from typing import Protocol

from af.framework.errors import PlatformError, PlatformReason

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
    "DriverResolutionError",
    "DriverTarget",
    "FailoverDriver",
    "FallbackDriver",
    "HealthStatus",
    "LoadBalancingDriver",
    "Manager",
    "ManagerConfigurationError",
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

    def __call__(self, ref: ConnectionRef, config: Mapping[str, object]) -> T_co: ...


class CapabilityVerifier[T_contra](Protocol):
    """Trusted verifier kept separate from the driver's self-report."""

    def __call__(
        self,
        ref: ConnectionRef,
        driver: T_contra,
        expected_binding: CapabilityBinding,
    ) -> CapabilityEvidence: ...


type DriverDisposer[T] = Callable[[T], object]


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
        self._clock = clock or (lambda: datetime.now(UTC))
        self._registrations: dict[str, _DriverRegistration[T]] = {}
        self._cache: dict[str, _CachedDriver[T]] = {}
        self._quarantined: set[str] = set()
        self._active: set[str] = set()
        self._resolution_order: list[str] = []
        self._lock = RLock()

    @property
    def manager_id(self) -> str:
        return self._manager_id

    @property
    def resolved_connections(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._cache))

    @property
    def quarantined_connections(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._quarantined))

    def evidence(self, name: str | None = None) -> CapabilityEvidence:
        """Return the last independently verified evidence for a resolved connection."""
        with self._lock:
            connection_name = self._requested_name(name)
            cached = self._cache.get(connection_name)
            if cached is None:
                raise DriverResolutionError(
                    protected_cause=LookupError("connection has no verified evidence")
                )
            return cached.evidence

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
            if not callable(factory) or not callable(verifier):
                raise ValueError("driver factory and verifier must be callable")
            if not isinstance(manifest, DriverManifest) or manifest.driver_name != normalized:
                raise ValueError("driver manifest identity does not match registration")
            if disposer is not None and not callable(disposer):
                raise ValueError("driver disposer must be callable")
        except (TypeError, ValueError) as error:
            raise ManagerConfigurationError(protected_cause=error) from error

        with self._lock:
            if normalized in self._registrations:
                cause = ValueError("driver factory is already registered")
                raise ManagerConfigurationError(protected_cause=cause) from cause
            self._registrations[normalized] = _DriverRegistration(
                factory,
                manifest,
                verifier,
                disposer,
            )

    def binding(self, name: str | None = None) -> CapabilityBinding:
        with self._lock:
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
            names = (
                tuple(dict.fromkeys((*reversed(self._resolution_order), *self._quarantined)))
                if name is None
                else (self._requested_name(name),)
            )
            failures: list[Exception] = []
            for connection_name in names:
                cached = self._cache.pop(connection_name, None)
                self._quarantined.discard(connection_name)
                if connection_name in self._resolution_order:
                    self._resolution_order.remove(connection_name)
                if cached is not None:
                    try:
                        self._dispose(cached)
                    except Exception as error:
                        failures.append(error)
            if failures:
                raise DriverResolutionError(
                    protected_cause=ExceptionGroup(
                        "driver disposal failed",
                        failures,
                    ),
                    cleanup_failures=tuple(failures),
                )

    def close(self) -> None:
        self.purge()

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
        try:
            driver = registration.factory(definition.ref, definition.config)
        except Exception as error:
            raise DriverResolutionError(protected_cause=error) from error
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
        self._cache[definition.ref.connection_name] = cached
        self._resolution_order.append(definition.ref.connection_name)
        return driver

    def _reuse_cached(
        self,
        definition: _ConnectionDefinition,
        cached: _CachedDriver[T],
        required: CapabilitySet,
    ) -> T:
        try:
            current = self._verify(definition, cached.driver, cached.registration)
        except CapabilityValidationError as error:
            cleanup = self._quarantine(definition.ref.connection_name, cached)
            raise ConnectionQuarantinedError(
                protected_cause=error.protected_cause,
                cleanup_failures=(() if cleanup is None else (cleanup,)),
            ) from error
        if current.material_fingerprint != cached.evidence.material_fingerprint:
            cleanup = self._quarantine(definition.ref.connection_name, cached)
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
            cleanup = self._quarantine(definition.ref.connection_name, cached)
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
        cached.evidence = current
        return cached.driver

    def _verify(
        self,
        definition: _ConnectionDefinition,
        driver: T,
        registration: _DriverRegistration[T],
    ) -> CapabilityEvidence:
        expected = self._binding(definition, registration.manifest)
        try:
            evidence = registration.verifier(definition.ref, driver, expected)
        except Exception as error:
            raise CapabilityValidationError(protected_cause=error) from error
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
        now = _aware_utc(self._clock(), "manager clock")
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
        self._cache.pop(name, None)
        if name in self._resolution_order:
            self._resolution_order.remove(name)
        self._quarantined.add(name)
        return self._try_dispose(cached)

    @staticmethod
    def _dispose(cached: _CachedDriver[T]) -> None:
        Manager._dispose_driver(cached.registration, cached.driver)

    @staticmethod
    def _dispose_driver(registration: _DriverRegistration[T], driver: T) -> None:
        if registration.disposer is not None:
            registration.disposer(driver)

    @staticmethod
    def _try_dispose(cached: _CachedDriver[T]) -> Exception | None:
        return Manager._try_dispose_driver(cached.registration, cached.driver)

    @staticmethod
    def _try_dispose_driver(
        registration: _DriverRegistration[T],
        driver: T,
    ) -> Exception | None:
        try:
            Manager._dispose_driver(registration, driver)
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
            self._index += 1
            current = self._targets[self._index]
            self._evidence.append(
                DegradationEvidence(
                    previous.ref,
                    current.ref,
                    reason_code,
                    self._clock(),
                )
            )
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
