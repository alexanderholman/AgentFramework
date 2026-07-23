from __future__ import annotations

from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from af.framework.application import Application, ApplicationState
from af.framework.container import (
    Container,
    ProviderRegistrar,
    ProviderResourceRegistry,
    ServiceProvider,
)
from af.framework.errors import ApplicationBootError, ApplicationRegistrationError
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    DriverResolutionError,
    HealthStatus,
    Manager,
    bind_driver,
    bind_manager,
)
from af.framework.packages import ManifestProviderAdapter, PackageProviderDeclaration

_NOW = datetime(2026, 7, 23, 20, tzinfo=UTC)
_ARTIFACT_DIGEST = "a" * 64
_DEPENDENCY_DIGEST = "b" * 64
_EVIDENCE_DIGEST = "c" * 64


@dataclass(frozen=True, slots=True)
class _Driver:
    connection_name: str


def _manifest(driver_name: str = "remote") -> DriverManifest:
    return DriverManifest(
        driver_name=driver_name,
        version="1.0.0",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=CapabilitySet("transactions"),
    )


def _evidence(binding: CapabilityBinding) -> CapabilityEvidence:
    return CapabilityEvidence(
        binding=binding,
        capabilities=CapabilitySet("transactions"),
        health=HealthStatus.HEALTHY,
        observed_at=_NOW,
        expires_at=_NOW + timedelta(minutes=5),
        evidence_digest=_EVIDENCE_DIGEST,
    )


def _registration(
    events: list[str],
    *,
    manager_id: str = "database",
    driver_name: str = "remote",
    label: str = "remote",
) -> DriverRegistration[_Driver]:
    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> _Driver:
        del config
        events.append(f"factory:{label}:{ref.connection_name}")
        return _Driver(ref.connection_name)

    def dispose(driver: _Driver) -> None:
        events.append(f"dispose:{driver.connection_name}")

    return DriverRegistration(
        manager_id=manager_id,
        driver_name=driver_name,
        factory=factory,
        manifest=_manifest(driver_name),
        verifier=lambda _ref, _driver, binding: _evidence(binding),
        disposer=dispose,
    )


class _LifecycleProvider(ServiceProvider):
    def __init__(
        self,
        provider_id: str,
        events: list[str],
        *,
        dependencies: tuple[str, ...] = (),
    ) -> None:
        self.provider_id = provider_id
        self.dependencies = dependencies
        self._events = events

    def register(self, registrar: ProviderRegistrar) -> None:
        del registrar
        self._events.append(f"register:{self.provider_id}")

    def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
        del container, resources
        self._events.append(f"boot:{self.provider_id}")

    def shutdown(self, container: Container) -> None:
        del container
        self._events.append(f"shutdown:{self.provider_id}")


class _DriverContributor(ServiceProvider):
    def __init__(
        self,
        provider_id: str,
        registration: DriverRegistration[_Driver],
        events: list[str],
        *,
        dependencies: tuple[str, ...] = (),
    ) -> None:
        self.provider_id = provider_id
        self.dependencies = dependencies
        self._registration = registration
        self._events = events

    def register(self, registrar: ProviderRegistrar) -> None:
        self._events.append(f"register:{self.provider_id}")
        bind_driver(registrar, self._registration)

    def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
        del container, resources
        self._events.append(f"boot:{self.provider_id}")

    def shutdown(self, container: Container) -> None:
        del container
        self._events.append(f"shutdown:{self.provider_id}")


class _ManagerProvider(ServiceProvider):
    def __init__(
        self,
        events: list[str],
        *,
        connections: Mapping[str, object],
        dependencies: tuple[str, ...] = (),
        resolve_names: tuple[str, ...] = (),
    ) -> None:
        self.provider_id = "manager.database"
        self.dependencies = dependencies
        self._events = events
        self._connections = connections
        self._resolve_names = resolve_names
        self.manager: Manager[_Driver] | None = None
        self.drivers: dict[str, _Driver] = {}

    def register(self, registrar: ProviderRegistrar) -> None:
        self._events.append(f"register:{self.provider_id}")
        bind_manager(
            registrar,
            manager_id="database",
            connections=self._connections,
            default="primary",
            deployment_profile="production",
            policy_version="policy-v1",
            clock=lambda: _NOW,
        )

    def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
        del resources
        self._events.append(f"boot:{self.provider_id}")
        manager = cast(Manager[_Driver], container.resolve(Manager, qualifier="database"))
        self.manager = manager
        for name in self._resolve_names:
            self.drivers[name] = manager.connection(name, requires="transactions")

    def shutdown(self, container: Container) -> None:
        del container
        self._events.append(f"shutdown:{self.provider_id}")


class _FailingProvider(ServiceProvider):
    provider_id = "service.failing"
    dependencies = ("manager.database",)

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def register(self, registrar: ProviderRegistrar) -> None:
        del registrar
        self._events.append(f"register:{self.provider_id}")

    def boot(self, container: Container, resources: ProviderResourceRegistry) -> None:
        del container, resources
        self._events.append(f"boot:{self.provider_id}")
        raise RuntimeError("private later boot failure")

    def shutdown(self, container: Container) -> None:
        del container
        self._events.append(f"shutdown:{self.provider_id}")


def test_manifest_contributor_and_manager_follow_application_lifecycle_order() -> None:
    events: list[str] = []
    resource_provider = _LifecycleProvider("package.resources", events)
    package_contributor = _DriverContributor(
        "driver.remote",
        _registration(events),
        events,
    )
    direct_contributor = _DriverContributor(
        "driver.local",
        _registration(events, driver_name="local", label="local"),
        events,
        dependencies=(resource_provider.provider_id,),
    )
    manifest_contributor = ManifestProviderAdapter(
        PackageProviderDeclaration(
            "driver.remote",
            "fixture.provider:DriverContributor",
        ),
        package_contributor,
        resource_provider_id=resource_provider.provider_id,
    )
    manager_provider = _ManagerProvider(
        events,
        connections={
            "primary": {"driver": "remote", "dsn": "postgresql://primary"},
            "analytics": {"driver": "local", "dsn": "sqlite://analytics"},
        },
        dependencies=(
            manifest_contributor.provider_id,
            direct_contributor.provider_id,
        ),
        resolve_names=("primary", "analytics"),
    )
    application = Application(
        Container(),
        (
            manager_provider,
            manifest_contributor,
            direct_contributor,
            resource_provider,
        ),
    )

    application.start()

    assert application.provider_order == (
        "package.resources",
        "driver.local",
        "driver.remote",
        "manager.database",
    )
    assert events[:8] == [
        "register:package.resources",
        "register:driver.local",
        "register:driver.remote",
        "register:manager.database",
        "boot:package.resources",
        "boot:driver.local",
        "boot:driver.remote",
        "boot:manager.database",
    ]
    assert manager_provider.manager is application.container.resolve(
        Manager,
        qualifier="database",
    )
    assert manager_provider.drivers == {
        "primary": _Driver("primary"),
        "analytics": _Driver("analytics"),
    }
    assert manager_provider.manager.connection("primary") is manager_provider.drivers["primary"]
    assert manager_provider.manager.connection("analytics") is manager_provider.drivers["analytics"]

    application.shutdown()
    application.shutdown()

    assert events[-6:] == [
        "shutdown:manager.database",
        "shutdown:driver.remote",
        "shutdown:driver.local",
        "shutdown:package.resources",
        "dispose:analytics",
        "dispose:primary",
    ]
    assert events.count("dispose:primary") == 1
    assert events.count("dispose:analytics") == 1


def test_duplicate_driver_contributors_fail_deterministically_before_factories_run() -> None:
    events: list[str] = []
    first = _DriverContributor(
        "contributor.a",
        _registration(events, label="first"),
        events,
    )
    second = _DriverContributor(
        "contributor.b",
        _registration(events, label="second"),
        events,
    )
    application = Application(Container(), (second, first))

    with pytest.raises(ApplicationRegistrationError) as captured:
        application.register()

    assert captured.value.provider_id == "contributor.b"
    assert events == ["register:contributor.a", "register:contributor.b"]
    assert not any(event.startswith("factory:") for event in events)
    assert application.state is ApplicationState.FAILED


def test_missing_configured_driver_registration_fails_boot_closed() -> None:
    events: list[str] = []
    manager_provider = _ManagerProvider(
        events,
        connections={"primary": {"driver": "missing"}},
    )
    application = Application(Container(), (manager_provider,))

    with pytest.raises(ApplicationBootError) as captured:
        application.start()

    assert captured.value.provider_id == "manager.database"
    assert isinstance(captured.value.protected_cause, DriverResolutionError)
    assert events == [
        "register:manager.database",
        "boot:manager.database",
        "shutdown:manager.database",
    ]
    assert manager_provider.manager is None
    assert application.state is ApplicationState.FAILED


def test_later_boot_failure_disposes_constructed_driver_once_after_provider_rollback() -> None:
    events: list[str] = []
    contributor = _DriverContributor(
        "driver.remote",
        _registration(events),
        events,
    )
    manager_provider = _ManagerProvider(
        events,
        connections={"primary": {"driver": "remote"}},
        dependencies=(contributor.provider_id,),
        resolve_names=("primary",),
    )
    failing = _FailingProvider(events)
    application = Application(Container(), (failing, manager_provider, contributor))

    with pytest.raises(ApplicationBootError) as captured:
        application.start()

    assert captured.value.provider_id == "service.failing"
    assert events.count("factory:remote:primary") == 1
    assert events.count("dispose:primary") == 1
    assert events[-4:] == [
        "shutdown:service.failing",
        "shutdown:manager.database",
        "shutdown:driver.remote",
        "dispose:primary",
    ]

    application.shutdown()
    application.shutdown()
    assert events.count("dispose:primary") == 1
    assert events.count("shutdown:manager.database") == 1
    assert application.state is ApplicationState.FAILED


def test_driver_registration_is_frozen_redacted_and_validated() -> None:
    events: list[str] = []
    registration = _registration(events)

    with pytest.raises(FrozenInstanceError):
        registration.manager_id = "other"  # type: ignore[misc]

    rendered = repr(registration)
    assert "[REDACTED]" in rendered
    assert "database" not in rendered
    assert "remote" not in rendered
    assert _ARTIFACT_DIGEST not in rendered

    invalid_changes: tuple[dict[str, object], ...] = (
        {"manager_id": "Bad ID"},
        {"driver_name": "Bad ID"},
        {"factory": object()},
        {"manifest": _manifest("different")},
        {"verifier": object()},
        {"disposer": object()},
    )
    for change in invalid_changes:
        with pytest.raises(ValueError):
            replace(registration, **change)  # type: ignore[arg-type]
