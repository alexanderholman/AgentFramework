from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import pytest

from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.framework.managers import CapabilitySet, DriverRegistration
from af.testing import (
    ConformanceFailure,
    ConformanceKind,
    ConformanceReport,
    DriverConformanceFixture,
    ProviderConformanceFixture,
    ProviderProbe,
    RealServiceHook,
    run_driver_conformance,
    run_provider_conformance,
)
from tests.fixtures.conformance_extension import (
    DRIVER_FIXTURE,
    PROVIDER_FIXTURE,
    REAL_SERVICE_CALLS,
    FixtureDriver,
    FixtureProvider,
    FixtureService,
)


def test_fixture_package_runs_provider_and_driver_conformance() -> None:
    provider = run_provider_conformance(PROVIDER_FIXTURE)
    driver = run_driver_conformance(DRIVER_FIXTURE)

    assert provider.kind is ConformanceKind.PROVIDER
    assert provider.checks == (
        "provider.lifecycle",
        "provider.parallel-isolation",
        "provider.cleanup",
        "provider.probe-1",
    )
    assert not provider.certifies_production
    assert driver.kind is ConformanceKind.DRIVER
    assert "driver.capability-evidence" in driver.checks
    assert driver.skipped_real_service_checks == ("fixture-real-service",)
    assert driver.real_service_checks == ()
    assert not driver.certifies_production
    assert REAL_SERVICE_CALLS == []


def test_real_service_hooks_require_explicit_opt_in() -> None:
    REAL_SERVICE_CALLS.clear()

    report = run_driver_conformance(DRIVER_FIXTURE, include_real_services=True)

    assert report.real_service_checks == ("fixture-real-service",)
    assert report.skipped_real_service_checks == ()
    assert REAL_SERVICE_CALLS == ["ran"]


@dataclass(slots=True)
class _SharedService:
    value: str = "shared"


_SHARED = _SharedService()


class _LeakingProvider(ServiceProvider):
    provider_id = "fixture.leaking"

    def register(self, registrar: ProviderRegistrar) -> None:
        registrar.bind(
            _SharedService,
            lambda _resolver: _SHARED,
            scope=Scope.APPLICATION,
            owned=False,
        )


def test_provider_conformance_detects_cross_application_global_state() -> None:
    fixture = ProviderConformanceFixture(
        name="leaking-provider",
        provider_factory=_LeakingProvider,
        probes=(ProviderProbe(_SharedService),),
    )

    with pytest.raises(ConformanceFailure, match="leaked one instance"):
        run_provider_conformance(fixture)


def test_provider_probe_can_explicitly_allow_shared_immutable_state() -> None:
    fixture = ProviderConformanceFixture(
        name="shared-provider",
        provider_factory=_LeakingProvider,
        probes=(ProviderProbe(_SharedService, require_isolation=False),),
    )

    report = run_provider_conformance(fixture)

    assert report.checks[-1] == "provider.probe-1"


def test_provider_fixture_repr_redacts_tokens_and_callbacks() -> None:
    rendered = repr(PROVIDER_FIXTURE)
    probe = repr(PROVIDER_FIXTURE.probes[0])

    assert "FixtureService" not in rendered
    assert "lambda" not in rendered
    assert "probes=1" in rendered
    assert "FixtureService" not in probe
    assert "[REDACTED]" in probe


@pytest.mark.parametrize(
    "build",
    [
        lambda: ProviderProbe(object(), require_isolation=1),
        lambda: ProviderConformanceFixture("Bad Name", FixtureProvider, PROVIDER_FIXTURE.probes),
        lambda: ProviderConformanceFixture("empty", FixtureProvider, ()),
        lambda: ProviderConformanceFixture(
            "bad-probe",
            FixtureProvider,
            cast(tuple[ProviderProbe, ...], (object(),)),
        ),
        lambda: RealServiceHook("Bad Name", lambda: None),
        lambda: ConformanceReport("bad", cast(ConformanceKind, "provider"), ("check",)),
        lambda: ConformanceReport("bad", ConformanceKind.PROVIDER, ()),
        lambda: ConformanceReport("bad", ConformanceKind.PROVIDER, ("Bad Check",)),
    ],
)
def test_conformance_values_reject_invalid_contracts(build: object) -> None:
    with pytest.raises(ValueError):
        cast(object, build)()


def test_async_callbacks_and_awaitable_results_fail_closed() -> None:
    async def callback() -> None:
        return None

    with pytest.raises(ValueError, match="synchronous"):
        ProviderConformanceFixture("async-provider", callback, PROVIDER_FIXTURE.probes)
    with pytest.raises(ValueError, match="synchronous"):
        RealServiceHook("async-hook", callback)

    def awaitable_probe(_service: object) -> object:
        return callback()

    fixture = ProviderConformanceFixture(
        "awaitable-provider",
        FixtureProvider,
        (ProviderProbe(FixtureService, exercise=awaitable_probe),),
    )
    with pytest.raises(TypeError, match="returned an awaitable"):
        run_provider_conformance(fixture)


def test_provider_factory_and_dependency_contracts_fail_closed() -> None:
    invalid_factory = cast(object, lambda: object())
    fixture = ProviderConformanceFixture(
        "invalid-provider",
        cast(object, invalid_factory),
        PROVIDER_FIXTURE.probes,
    )
    with pytest.raises(ConformanceFailure, match="ServiceProvider"):
        run_provider_conformance(fixture)

    class InvalidDependencies(FixtureProvider):
        dependencies = ["invalid"]

    fixture = ProviderConformanceFixture(
        "invalid-dependencies",
        InvalidDependencies,
        PROVIDER_FIXTURE.probes,
    )
    with pytest.raises(ConformanceFailure, match="dependencies"):
        run_provider_conformance(fixture)


def test_provider_conformance_detects_nondeterministic_observations() -> None:
    counter = iter(("first", "second"))

    class VariableService:
        def __init__(self) -> None:
            self.value = next(counter)

    class VariableProvider(ServiceProvider):
        provider_id = "fixture.variable"

        def register(self, registrar: ProviderRegistrar) -> None:
            registrar.bind(
                VariableService,
                lambda _resolver: VariableService(),
                scope=Scope.APPLICATION,
                owned=False,
            )

    fixture = ProviderConformanceFixture(
        "variable-provider",
        VariableProvider,
        (
            ProviderProbe(
                VariableService,
                exercise=lambda value: cast(VariableService, value).value,
            ),
        ),
    )
    with pytest.raises(ConformanceFailure, match="non-deterministic"):
        run_provider_conformance(fixture)


def test_driver_fixture_snapshots_configuration_and_redacts_callbacks() -> None:
    config = {"endpoint": "fixture://local"}
    fixture = DriverConformanceFixture(
        name="snapshot-driver",
        registration_factory=DRIVER_FIXTURE.registration_factory,
        manager_id="fixture",
        connection_name="primary",
        connection_config=config,
        required_capabilities=CapabilitySet("fixture.read"),
        exercise=lambda driver: cast(FixtureDriver, driver).connection_name,
        clock=lambda: datetime(2026, 7, 23, 22, tzinfo=UTC),
    )
    config["endpoint"] = "mutated"

    assert fixture.connection_config["endpoint"] == "fixture://local"
    assert "registration_factory" not in repr(fixture)
    assert "lambda" not in repr(fixture)
    assert "[REDACTED]" in repr(RealServiceHook("redacted-hook", lambda: None))


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Bad Name"},
        {"manager_id": "Bad Name"},
        {"connection_name": "Bad Name"},
        {"deployment_profile": "Bad Name"},
        {"policy_version": ""},
        {"connection_config": {"driver": "override"}},
        {"connection_config": cast(object, ())},
        {"required_capabilities": cast(object, ())},
        {"real_service_hooks": cast(object, (object(),))},
    ],
)
def test_driver_fixture_rejects_invalid_contracts(changes: dict[str, object]) -> None:
    values: dict[str, object] = {
        "name": "valid-driver",
        "registration_factory": DRIVER_FIXTURE.registration_factory,
        "manager_id": "fixture",
        "connection_name": "primary",
    }
    values.update(changes)

    with pytest.raises(ValueError):
        DriverConformanceFixture(**values)  # type: ignore[arg-type]


def test_driver_registration_contracts_fail_closed() -> None:
    invalid = DriverConformanceFixture(
        "invalid-registration",
        cast(object, lambda: object()),
        "fixture",
        "primary",
    )
    with pytest.raises(ConformanceFailure, match="DriverRegistration"):
        run_driver_conformance(invalid)

    def wrong_manager() -> DriverRegistration[FixtureDriver]:
        registration = DRIVER_FIXTURE.registration_factory()
        return DriverRegistration(
            manager_id="other",
            driver_name=registration.driver_name,
            factory=registration.factory,
            manifest=registration.manifest,
            verifier=registration.verifier,
            disposer=registration.disposer,
        )

    mismatch = DriverConformanceFixture(
        "mismatched-registration",
        wrong_manager,
        "fixture",
        "primary",
    )
    with pytest.raises(ConformanceFailure, match="manager identity"):
        run_driver_conformance(mismatch)


def test_driver_conformance_validates_public_arguments_and_hook_results() -> None:
    with pytest.raises(TypeError, match="DriverConformanceFixture"):
        run_driver_conformance(cast(object, object()))
    with pytest.raises(ValueError, match="boolean"):
        run_driver_conformance(DRIVER_FIXTURE, include_real_services=cast(bool, 1))

    async def result() -> None:
        return None

    def awaitable_hook() -> object:
        return result()

    fixture = DriverConformanceFixture(
        name="awaitable-hook-driver",
        registration_factory=DRIVER_FIXTURE.registration_factory,
        manager_id="fixture",
        connection_name="primary",
        connection_config={"endpoint": "fixture://local"},
        required_capabilities=CapabilitySet("fixture.read"),
        real_service_hooks=(RealServiceHook("awaitable-result", awaitable_hook),),
        clock=DRIVER_FIXTURE.clock,
    )
    with pytest.raises(TypeError, match="returned an awaitable"):
        run_driver_conformance(fixture, include_real_services=True)


def test_provider_conformance_validates_public_argument() -> None:
    with pytest.raises(TypeError, match="ProviderConformanceFixture"):
        run_provider_conformance(cast(object, object()))
