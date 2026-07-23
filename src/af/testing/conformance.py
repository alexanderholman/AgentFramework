"""Reusable provider and driver conformance harnesses for AF extensions."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from inspect import isasyncgenfunction, isawaitable, iscoroutinefunction
from types import MappingProxyType
from typing import cast

from af.framework.container import ProviderRegistrar, Qualifier, ServiceProvider
from af.framework.managers import (
    CapabilitySet,
    DriverRegistration,
    Manager,
    bind_driver,
    bind_manager,
)
from af.testing.application import TestApplicationBuilder

__all__ = (
    "ConformanceFailure",
    "ConformanceKind",
    "ConformanceReport",
    "DriverConformanceFixture",
    "ProviderConformanceFixture",
    "ProviderProbe",
    "RealServiceHook",
    "run_driver_conformance",
    "run_provider_conformance",
)

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9_.-]{0,126}[a-z0-9])?$")
_MAX_PROBES = 128
_MAX_HOOKS = 128
_UNSET = object()


class ConformanceFailure(AssertionError):
    """A deterministic extension conformance requirement was not met."""


class ConformanceKind(StrEnum):
    PROVIDER = "provider"
    DRIVER = "driver"


def _name(value: object, field_name: str) -> str:
    if type(value) is not str or _NAME.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical identifier")
    return value


def _sync_callable(value: object, field_name: str) -> None:
    if not callable(value):
        raise ValueError(f"{field_name} must be a synchronous callable")
    if iscoroutinefunction(value) or isasyncgenfunction(value):
        raise ValueError(f"{field_name} must be a synchronous callable")
    try:
        call = type(value).__call__
    except Exception as error:
        raise ValueError(f"{field_name} callback contract cannot be inspected") from error
    if iscoroutinefunction(call) or isasyncgenfunction(call):
        raise ValueError(f"{field_name} must be a synchronous callable")


def _snapshot(value: object, field_name: str) -> object:
    if isawaitable(value):
        close = getattr(value, "close", None)
        cancel = getattr(value, "cancel", None)
        if callable(close):
            close()
        elif callable(cancel):
            cancel()
        raise TypeError(f"{field_name} returned an awaitable")
    try:
        return deepcopy(value)
    except Exception as error:
        raise ValueError(f"{field_name} must support deterministic snapshotting") from error


@dataclass(frozen=True, slots=True, repr=False)
class ProviderProbe:
    """One application binding to resolve and optionally exercise in each test host."""

    token: object
    qualifier: Qualifier | str | None = None
    exercise: Callable[[object], object] | None = None
    require_isolation: bool = True

    def __post_init__(self) -> None:
        if self.exercise is not None:
            _sync_callable(self.exercise, "provider probe exercise")
        if type(self.require_isolation) is not bool:
            raise ValueError("provider probe isolation flag must be boolean")

    def __repr__(self) -> str:
        return (
            "ProviderProbe(token='[REDACTED]', qualifier='[REDACTED]', "
            f"exercise={'set' if self.exercise is not None else 'none'}, "
            f"require_isolation={self.require_isolation!r})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ProviderConformanceFixture:
    """Factory and binding probes for one extension service provider."""

    name: str
    provider_factory: Callable[[], ServiceProvider]
    probes: tuple[ProviderProbe, ...]

    def __post_init__(self) -> None:
        _name(self.name, "provider fixture name")
        _sync_callable(self.provider_factory, "provider fixture factory")
        if not self.probes or len(self.probes) > _MAX_PROBES:
            raise ValueError("provider fixture must declare a bounded non-empty probe set")
        if any(not isinstance(probe, ProviderProbe) for probe in self.probes):
            raise ValueError("provider fixture probes must be ProviderProbe values")

    def __repr__(self) -> str:
        return f"ProviderConformanceFixture(name={self.name!r}, probes={len(self.probes)})"


@dataclass(frozen=True, slots=True, repr=False)
class RealServiceHook:
    """Explicit opt-in integration hook; never runs during default fake conformance."""

    name: str
    run: Callable[[], object]

    def __post_init__(self) -> None:
        _name(self.name, "real-service hook name")
        _sync_callable(self.run, "real-service hook")

    def __repr__(self) -> str:
        return f"RealServiceHook(name={self.name!r}, run='[REDACTED]')"


@dataclass(frozen=True, slots=True, repr=False)
class DriverConformanceFixture[T]:
    """One named-driver registration exercised through a real AF Manager."""

    name: str
    registration_factory: Callable[[], DriverRegistration[T]]
    manager_id: str
    connection_name: str
    connection_config: Mapping[str, object] = field(default_factory=dict)
    required_capabilities: CapabilitySet = field(default_factory=CapabilitySet)
    exercise: Callable[[T], object] | None = None
    real_service_hooks: tuple[RealServiceHook, ...] = ()
    deployment_profile: str = "test"
    policy_version: str = "test-v1"
    clock: Callable[[], datetime] | None = None

    def __post_init__(self) -> None:
        _name(self.name, "driver fixture name")
        _name(self.manager_id, "driver fixture manager id")
        _name(self.connection_name, "driver fixture connection name")
        _name(self.deployment_profile, "driver fixture deployment profile")
        if type(self.policy_version) is not str or not self.policy_version:
            raise ValueError("driver fixture policy version must be non-empty")
        _sync_callable(self.registration_factory, "driver registration factory")
        if self.exercise is not None:
            _sync_callable(self.exercise, "driver fixture exercise")
        if self.clock is not None:
            _sync_callable(self.clock, "driver fixture clock")
        if not isinstance(self.connection_config, Mapping):
            raise ValueError("driver fixture connection config must be a mapping")
        try:
            config = MappingProxyType(deepcopy(dict(self.connection_config)))
        except Exception as error:
            raise ValueError("driver fixture connection config must be snapshot-safe") from error
        if "driver" in config:
            raise ValueError("driver fixture connection config must not override driver identity")
        if not isinstance(self.required_capabilities, CapabilitySet):
            raise ValueError("driver fixture capabilities must be a CapabilitySet")
        if len(self.real_service_hooks) > _MAX_HOOKS or any(
            not isinstance(hook, RealServiceHook) for hook in self.real_service_hooks
        ):
            raise ValueError("driver fixture real-service hooks are invalid")
        object.__setattr__(self, "connection_config", config)

    def __repr__(self) -> str:
        return (
            "DriverConformanceFixture("
            f"name={self.name!r}, manager_id={self.manager_id!r}, "
            f"connection_name={self.connection_name!r}, "
            f"real_service_hooks={len(self.real_service_hooks)})"
        )


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """Bounded result from one conformance run; never production certification."""

    fixture: str
    kind: ConformanceKind
    checks: tuple[str, ...]
    real_service_checks: tuple[str, ...] = ()
    skipped_real_service_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _name(self.fixture, "conformance report fixture")
        if not isinstance(self.kind, ConformanceKind):
            raise ValueError("conformance report kind is invalid")
        if not self.checks:
            raise ValueError("conformance report requires at least one check")
        for value in (*self.checks, *self.real_service_checks, *self.skipped_real_service_checks):
            _name(value, "conformance check")

    @property
    def certifies_production(self) -> bool:
        """Unit/contract conformance is necessary evidence, never deployment authority."""
        return False


@dataclass(slots=True)
class _ProviderRun:
    provider: ServiceProvider
    provider_id: str
    dependencies: tuple[str, ...]
    values: tuple[object, ...]
    observations: tuple[object | None, ...]


def _run_provider_once(fixture: ProviderConformanceFixture) -> _ProviderRun:
    provider = fixture.provider_factory()
    if not isinstance(provider, ServiceProvider):
        raise ConformanceFailure("provider factory did not return a ServiceProvider")
    provider_id = getattr(provider, "provider_id", None)
    dependencies = getattr(provider, "dependencies", ())
    if type(provider_id) is not str:
        raise ConformanceFailure("provider identity is not a string")
    if type(dependencies) is not tuple or any(type(value) is not str for value in dependencies):
        raise ConformanceFailure("provider dependencies are not a tuple of identifiers")

    application = TestApplicationBuilder().provider(provider).build()
    values: list[object] = []
    observations: list[object | None] = []
    try:
        for probe in fixture.probes:
            value = application.resolve_token(probe.token, qualifier=probe.qualifier)
            values.append(value)
            if probe.exercise is None:
                observations.append(None)
            else:
                observations.append(_snapshot(probe.exercise(value), "provider probe exercise"))
    finally:
        application.shutdown()
        application.assert_clean()
    return _ProviderRun(provider, provider_id, dependencies, tuple(values), tuple(observations))


def run_provider_conformance(fixture: ProviderConformanceFixture) -> ConformanceReport:
    """Run two provider hosts concurrently and prove deterministic isolation and cleanup."""

    if not isinstance(fixture, ProviderConformanceFixture):
        raise TypeError("provider conformance requires a ProviderConformanceFixture")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="af-provider-conformance") as pool:
        futures = tuple(pool.submit(_run_provider_once, fixture) for _ in range(2))
        first = futures[0].result()
        second = futures[1].result()

    if first.provider is second.provider:
        raise ConformanceFailure(
            "provider factory leaked one provider instance across applications"
        )
    if first.provider_id != second.provider_id or first.dependencies != second.dependencies:
        raise ConformanceFailure("provider identity or dependencies changed between applications")
    checks = ["provider.lifecycle", "provider.parallel-isolation", "provider.cleanup"]
    for index, probe in enumerate(fixture.probes):
        if probe.require_isolation and first.values[index] is second.values[index]:
            raise ConformanceFailure("provider binding leaked one instance across applications")
        if probe.exercise is not None and first.observations[index] != second.observations[index]:
            raise ConformanceFailure("provider probe produced non-deterministic observations")
        checks.append(f"provider.probe-{index + 1}")
    return ConformanceReport(fixture.name, ConformanceKind.PROVIDER, tuple(checks))


class _DriverFixtureProvider[T](ServiceProvider):
    provider_id = "af.testing.conformance.driver"

    def __init__(
        self,
        fixture: DriverConformanceFixture[T],
        registration: DriverRegistration[T],
    ) -> None:
        self._fixture = fixture
        self._registration = registration

    def register(self, registrar: ProviderRegistrar) -> None:
        bind_driver(registrar, self._registration)
        config = dict(self._fixture.connection_config)
        config["driver"] = self._registration.driver_name
        bind_manager(
            registrar,
            manager_id=self._fixture.manager_id,
            connections={self._fixture.connection_name: config},
            default=self._fixture.connection_name,
            deployment_profile=self._fixture.deployment_profile,
            policy_version=self._fixture.policy_version,
            clock=self._fixture.clock,
        )


@dataclass(slots=True)
class _DriverRun[T]:
    manager: Manager[T]
    driver: T
    observation: object | None


def _run_driver_once[T](fixture: DriverConformanceFixture[T]) -> _DriverRun[T]:
    registration = fixture.registration_factory()
    if not isinstance(registration, DriverRegistration):
        raise ConformanceFailure("driver factory did not return a DriverRegistration")
    if registration.manager_id != fixture.manager_id:
        raise ConformanceFailure("driver registration manager identity does not match fixture")

    disposal_count = 0
    original_disposer = registration.disposer

    def dispose(driver: T) -> object:
        nonlocal disposal_count
        disposal_count += 1
        if original_disposer is None:
            return None
        return original_disposer(driver)

    tracked = replace(registration, disposer=dispose)
    provider = _DriverFixtureProvider(fixture, tracked)
    application = TestApplicationBuilder().provider(provider).build()
    manager_value: object = _UNSET
    driver_value: object = _UNSET
    observation: object | None = None
    try:
        manager = cast(Manager[T], application.resolve(Manager, qualifier=fixture.manager_id))
        manager_value = manager
        driver = manager.connection(
            fixture.connection_name,
            requires=fixture.required_capabilities,
        )
        driver_value = driver
        if (
            manager.connection(
                fixture.connection_name,
                requires=fixture.required_capabilities,
            )
            is not driver
        ):
            raise ConformanceFailure("manager did not cache one named driver deterministically")
        evidence = manager.evidence(fixture.connection_name)
        if evidence.binding != manager.binding(fixture.connection_name):
            raise ConformanceFailure("driver evidence is not bound to the configured connection")
        if not evidence.capabilities.supports(fixture.required_capabilities):
            raise ConformanceFailure("driver evidence does not support required capabilities")
        if fixture.exercise is not None:
            observation = _snapshot(fixture.exercise(driver), "driver fixture exercise")
    finally:
        application.shutdown()
        application.assert_clean()
    if disposal_count != 1:
        raise ConformanceFailure("resolved driver was not disposed exactly once")
    if manager_value is _UNSET or driver_value is _UNSET:
        raise ConformanceFailure("driver conformance did not resolve the configured connection")
    return _DriverRun(
        cast(Manager[T], manager_value),
        cast(T, driver_value),
        observation,
    )


def _run_real_service_hooks(
    hooks: tuple[RealServiceHook, ...],
    *,
    enabled: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    names = tuple(hook.name for hook in hooks)
    if not enabled:
        return (), names
    completed: list[str] = []
    for hook in hooks:
        result = hook.run()
        if isawaitable(result):
            close = getattr(result, "close", None)
            cancel = getattr(result, "cancel", None)
            if callable(close):
                close()
            elif callable(cancel):
                cancel()
            raise TypeError("real-service hook returned an awaitable")
        completed.append(hook.name)
    return tuple(completed), ()


def run_driver_conformance[T](
    fixture: DriverConformanceFixture[T],
    *,
    include_real_services: bool = False,
) -> ConformanceReport:
    """Run two named-driver hosts plus explicitly enabled real-service hooks."""

    if not isinstance(fixture, DriverConformanceFixture):
        raise TypeError("driver conformance requires a DriverConformanceFixture")
    if type(include_real_services) is not bool:
        raise ValueError("real-service inclusion flag must be boolean")
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="af-driver-conformance") as pool:
        futures = tuple(pool.submit(_run_driver_once, fixture) for _ in range(2))
        first = futures[0].result()
        second = futures[1].result()
    if first.manager is second.manager:
        raise ConformanceFailure("manager instance leaked across applications")
    if first.driver is second.driver:
        raise ConformanceFailure("driver instance leaked across applications")
    if first.observation != second.observation:
        raise ConformanceFailure("driver exercise produced non-deterministic observations")
    real_checks, skipped = _run_real_service_hooks(
        fixture.real_service_hooks,
        enabled=include_real_services,
    )
    checks = (
        "driver.lifecycle",
        "driver.parallel-isolation",
        "driver.named-cache",
        "driver.capability-evidence",
        "driver.cleanup",
    )
    return ConformanceReport(
        fixture.name,
        ConformanceKind.DRIVER,
        checks,
        real_checks,
        skipped,
    )
