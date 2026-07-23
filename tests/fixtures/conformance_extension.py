"""Small external-style provider and driver package for conformance evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)
from af.testing import (
    DriverConformanceFixture,
    ProviderConformanceFixture,
    ProviderProbe,
    RealServiceHook,
)

_NOW = datetime(2026, 7, 23, 22, tzinfo=UTC)
_ARTIFACT = "a" * 64
_DEPENDENCIES = "b" * 64
_EVIDENCE = "c" * 64


@dataclass(slots=True)
class FixtureService:
    value: str = "fixture-service"
    closed: bool = False

    def close(self) -> None:
        self.closed = True


class FixtureProvider(ServiceProvider):
    provider_id = "fixture.extension"

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            FixtureService,
            lambda _resolver: FixtureService(),
            scope=Scope.APPLICATION,
            owned=True,
        )


@dataclass(slots=True)
class FixtureDriver:
    connection_name: str
    closed: bool = False


def _driver_registration() -> DriverRegistration[FixtureDriver]:
    manifest = DriverManifest(
        driver_name="fixture",
        version="1.0.0",
        artifact_digest=_ARTIFACT,
        dependency_graph_digest=_DEPENDENCIES,
        capabilities=CapabilitySet(("fixture.read",)),
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> FixtureDriver:
        if config.get("endpoint") != "fixture://local":
            raise ValueError("fixture endpoint is invalid")
        return FixtureDriver(ref.connection_name)

    def verifier(
        ref: ConnectionRef,
        driver: FixtureDriver,
        binding: CapabilityBinding,
    ) -> CapabilityEvidence:
        if ref.connection_name != driver.connection_name:
            raise ValueError("fixture driver identity mismatch")
        return CapabilityEvidence(
            binding=binding,
            capabilities=CapabilitySet(("fixture.read",)),
            health=HealthStatus.HEALTHY,
            observed_at=_NOW,
            expires_at=_NOW + timedelta(minutes=5),
            evidence_digest=_EVIDENCE,
        )

    def dispose(driver: FixtureDriver) -> None:
        driver.closed = True

    return DriverRegistration(
        manager_id="fixture",
        driver_name="fixture",
        factory=factory,
        manifest=manifest,
        verifier=verifier,
        disposer=dispose,
    )


REAL_SERVICE_CALLS: list[str] = []


def _real_service_probe() -> None:
    REAL_SERVICE_CALLS.append("ran")


PROVIDER_FIXTURE = ProviderConformanceFixture(
    name="fixture-provider",
    provider_factory=FixtureProvider,
    probes=(
        ProviderProbe(
            FixtureService,
            exercise=lambda service: cast(FixtureService, service).value,
        ),
    ),
)

DRIVER_FIXTURE = DriverConformanceFixture(
    name="fixture-driver",
    registration_factory=_driver_registration,
    manager_id="fixture",
    connection_name="primary",
    connection_config={"endpoint": "fixture://local"},
    required_capabilities=CapabilitySet(("fixture.read",)),
    exercise=lambda driver: driver.connection_name,
    real_service_hooks=(RealServiceHook("fixture-real-service", _real_service_probe),),
    clock=lambda: _NOW,
)
