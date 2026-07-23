"""External-style provider and driver package used by AF conformance tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.testing import (
    ConformanceCase,
    DriverConformanceSuite,
    ProviderConformanceSuite,
    RealServiceHook,
    TestApplication,
)


@dataclass(slots=True)
class FixtureService:
    """One mutable application-scoped service owned by the fixture extension."""

    name: str
    values: list[str] = field(default_factory=list)


class FixtureProvider(ServiceProvider):
    """Small external-style provider with one application-scoped binding."""

    provider_id = "tests.fixture-extension"

    def __init__(self, service: FixtureService) -> None:
        self._service = service

    def register(self, registrar: ProviderRegistrar) -> None:
        service = self._service
        registrar.bind(
            FixtureService,
            lambda _resolver: service,
            scope=Scope.APPLICATION,
            owned=False,
        )


CREATED_SERVICES: list[FixtureService] = []
OBSERVED_SERVICES: list[FixtureService] = []
DRIVER_EVENTS: list[tuple[str, int]] = []


def reset_fixture_state() -> None:
    """Reset only test instrumentation; production APIs never depend on this state."""

    CREATED_SERVICES.clear()
    OBSERVED_SERVICES.clear()
    DRIVER_EVENTS.clear()


def provider_factory() -> ServiceProvider:
    service = FixtureService(f"service-{len(CREATED_SERVICES) + 1}")
    CREATED_SERVICES.append(service)
    return FixtureProvider(service)


def _resolve_service(application: TestApplication) -> None:
    service = application.resolve(FixtureService)
    service.values.append("observed")
    OBSERVED_SERVICES.append(service)
    assert service.values == ["observed"]


PROVIDER_SUITE = ProviderConformanceSuite(
    "fixture-provider",
    (
        ConformanceCase("resolves-fresh-service", _resolve_service),
        ConformanceCase("isolates-fresh-service", _resolve_service),
    ),
)


@dataclass(slots=True)
class FixtureDriver:
    """Minimal stateful driver used to prove per-case fixture isolation."""

    serial: int
    values: dict[str, str] = field(default_factory=dict)

    def write(self, key: str, value: str) -> None:
        self.values[key] = value

    def read(self, key: str) -> str | None:
        return self.values.get(key)


@contextmanager
def driver_fixture() -> Iterator[FixtureDriver]:
    serial = (len(DRIVER_EVENTS) // 2) + 1
    driver = FixtureDriver(serial)
    DRIVER_EVENTS.append(("open", serial))
    try:
        yield driver
    finally:
        DRIVER_EVENTS.append(("close", serial))


def _writes_and_reads(driver: FixtureDriver) -> None:
    driver.write("key", "value")
    assert driver.read("key") == "value"


def _starts_empty(driver: FixtureDriver) -> None:
    assert driver.read("key") is None


DRIVER_SUITE = DriverConformanceSuite(
    "fixture-driver",
    (
        ConformanceCase("writes-and-reads", _writes_and_reads),
        ConformanceCase("starts-empty", _starts_empty),
    ),
)

DISABLED_REAL_SERVICE = RealServiceHook(
    "fixture-real-service",
    driver_fixture,
    enabled=False,
    disabled_reason="external fixture service is not configured",
)
