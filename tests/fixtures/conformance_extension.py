"""Small external-style extension package for testing AF conformance APIs."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.testing import (
    ConformanceCase,
    DriverConformanceSuite,
    ProviderConformanceSuite,
    TestApplication,
)


@dataclass(slots=True)
class FixtureService:
    value: str = "fixture-service"


class FixtureProvider(ServiceProvider):
    provider_id = "fixture.extension"

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            FixtureService,
            lambda _resolver: FixtureService(),
            scope=Scope.APPLICATION,
            owned=False,
        )


@dataclass(slots=True)
class FixtureDriver:
    value: str = "fixture-driver"
    closed: bool = False


@contextmanager
def driver_fixture() -> Iterator[FixtureDriver]:
    driver = FixtureDriver()
    try:
        yield driver
    finally:
        driver.closed = True


def _resolve_service(application: TestApplication) -> None:
    assert application.resolve(FixtureService).value == "fixture-service"


def _exercise_driver(driver: FixtureDriver) -> None:
    assert driver.value == "fixture-driver"
    assert driver.closed is False


PROVIDER_SUITE = ProviderConformanceSuite(
    "fixture-provider",
    (ConformanceCase("resolves-service", _resolve_service),),
)

DRIVER_SUITE = DriverConformanceSuite(
    "fixture-driver",
    (ConformanceCase("exercises-driver", _exercise_driver),),
)
