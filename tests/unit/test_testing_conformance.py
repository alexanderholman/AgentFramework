from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from af.framework.container import ProviderRegistrar, Scope, ServiceProvider
from af.testing import (
    ConformanceCase,
    ConformanceEvidence,
    ConformanceReport,
    DriverConformanceSuite,
    IntegrationUnavailable,
    ProviderConformanceSuite,
    RealServiceHook,
    TestApplication,
    TestApplicationBuilder,
)


@dataclass(frozen=True, slots=True)
class _Value:
    name: str


class _ValueProvider(ServiceProvider):
    provider_id = "tests.conformance-value"

    def __init__(self, name: str) -> None:
        self._value = _Value(name)

    def register(self, registrar: ProviderRegistrar) -> None:
        value = self._value
        registrar.bind(
            _Value,
            lambda _resolver: value,
            scope=Scope.APPLICATION,
            owned=False,
        )


def test_provider_conformance_runs_each_case_in_a_fresh_owned_application() -> None:
    provider_names: list[str] = []
    application_ids: list[int] = []

    def provider() -> ServiceProvider:
        name = f"provider-{len(provider_names) + 1}"
        provider_names.append(name)
        return _ValueProvider(name)

    def inspect(application: TestApplication) -> None:
        application_ids.append(id(application.application))
        assert application.resolve(_Value).name in provider_names

    suite = ProviderConformanceSuite(
        "value-provider",
        (
            ConformanceCase("resolves", inspect),
            ConformanceCase("isolates", inspect),
        ),
    )

    report = suite.run(
        provider,
        builder_factory=TestApplicationBuilder,
    )

    assert report.suite == "value-provider"
    assert report.cases == ("resolves", "isolates")
    assert report.evidence is ConformanceEvidence.ISOLATED
    assert report.certifies_production is False
    assert provider_names == ["provider-1", "provider-2"]
    assert len(set(application_ids)) == 2


def test_driver_conformance_uses_a_fresh_fixture_and_always_cleans_up() -> None:
    events: list[str] = []

    @contextmanager
    def fixture() -> Iterator[list[str]]:
        driver: list[str] = []
        events.append("open")
        try:
            yield driver
        finally:
            events.append("close")

    def starts_empty(driver: list[str]) -> None:
        assert driver == []

    suite = DriverConformanceSuite(
        "list-driver",
        (
            ConformanceCase("starts-empty", starts_empty),
            ConformanceCase("mutates-locally", lambda driver: driver.append("value")),
        ),
    )

    report = suite.run(fixture)

    assert report.cases == ("starts-empty", "mutates-locally")
    assert report.evidence is ConformanceEvidence.FAKE
    assert report.certifies_production is False
    assert events == ["open", "close", "open", "close"]

    failing = DriverConformanceSuite(
        "failing-driver",
        (ConformanceCase("fails", lambda _driver: (_ for _ in ()).throw(ValueError("boom"))),),
    )
    with pytest.raises(ValueError, match="boom"):
        failing.run(fixture)
    assert events[-2:] == ["open", "close"]


def test_real_service_hooks_require_explicit_enablement_and_remain_non_certifying() -> None:
    calls: list[str] = []

    @contextmanager
    def real_fixture() -> Iterator[str]:
        calls.append("open")
        try:
            yield "real"
        finally:
            calls.append("close")

    def connects(driver: str) -> None:
        assert driver == "real"

    suite = DriverConformanceSuite(
        "service-driver",
        (ConformanceCase("connects", connects),),
    )
    disabled = RealServiceHook(
        "postgres-integration",
        real_fixture,
        enabled=False,
        disabled_reason="AF_TEST_POSTGRES is not configured",
    )

    with pytest.raises(IntegrationUnavailable, match="AF_TEST_POSTGRES"):
        suite.run_real(disabled)
    assert calls == []

    enabled = RealServiceHook(
        "postgres-integration",
        real_fixture,
        enabled=True,
    )
    report = suite.run_real(enabled)

    assert calls == ["open", "close"]
    assert report.integration == "postgres-integration"
    assert report.evidence is ConformanceEvidence.REAL_SERVICE
    assert report.certifies_production is False

    with pytest.raises(TypeError, match="unexpected keyword argument"):
        suite.run(real_fixture, evidence=ConformanceEvidence.REAL_SERVICE)  # type: ignore[call-arg]


@pytest.mark.parametrize("name", ["", "contains space", "contains\nnewline"])
def test_conformance_declarations_reject_ambiguous_names(name: str) -> None:
    with pytest.raises(ValueError):
        ConformanceCase(name, lambda _fixture: None)


def test_conformance_declarations_reject_invalid_or_ambiguous_configuration() -> None:
    case = ConformanceCase("valid", lambda _fixture: None)
    assert "REDACTED" in repr(case)

    with pytest.raises(ValueError, match="callable"):
        ConformanceCase("invalid", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-empty"):
        DriverConformanceSuite("empty", ())
    with pytest.raises(ValueError, match="unique"):
        DriverConformanceSuite("duplicate", (case, case))
    with pytest.raises(ValueError, match="cases are invalid"):
        ConformanceReport("report", (), ConformanceEvidence.FAKE)
    with pytest.raises(ValueError, match="evidence"):
        ConformanceReport("report", ("valid",), "fake")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="requires an integration"):
        ConformanceReport("report", ("valid",), ConformanceEvidence.REAL_SERVICE)
    with pytest.raises(ValueError, match="only real-service"):
        ConformanceReport("report", ("valid",), ConformanceEvidence.FAKE, "postgres")


def test_real_service_hook_validates_configuration_and_redacts_details() -> None:
    @contextmanager
    def fixture() -> Iterator[str]:
        yield "value"

    disabled = RealServiceHook("service", fixture, disabled_reason="not configured")
    assert "not configured" not in repr(disabled)
    assert "REDACTED" in repr(disabled)

    with pytest.raises(ValueError, match="fixture must be callable"):
        RealServiceHook("service", None, disabled_reason="not configured")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="boolean"):
        RealServiceHook("service", fixture, enabled=1, disabled_reason="not configured")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="bounded single-line"):
        RealServiceHook("service", fixture, disabled_reason="contains\nnewline")
    with pytest.raises(ValueError, match="cannot have"):
        RealServiceHook("service", fixture, enabled=True, disabled_reason="unused")
    with pytest.raises(TypeError, match="context manager"):
        RealServiceHook(
            "service",
            lambda: "not-a-context-manager",  # type: ignore[arg-type,return-value]
            enabled=True,
        ).open()


def test_suites_reject_invalid_factories_and_asynchronous_cases() -> None:
    provider_suite = ProviderConformanceSuite(
        "provider",
        (ConformanceCase("valid", lambda _application: None),),
    )
    with pytest.raises(ValueError, match="factories"):
        provider_suite.run(None, builder_factory=TestApplicationBuilder)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ServiceProvider"):
        provider_suite.run(lambda: object(), builder_factory=TestApplicationBuilder)  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="TestApplicationBuilder"):
        provider_suite.run(lambda: _ValueProvider("value"), builder_factory=object)  # type: ignore[arg-type]

    driver_suite = DriverConformanceSuite(
        "driver",
        (ConformanceCase("valid", lambda _driver: None),),
    )
    with pytest.raises(ValueError, match="factory must be callable"):
        driver_suite.run(None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="context manager"):
        driver_suite.run(lambda: object())  # type: ignore[arg-type,return-value]
    with pytest.raises(TypeError, match="RealServiceHook"):
        driver_suite.run_real(object())  # type: ignore[arg-type]

    async def asynchronous(_driver: str) -> None:
        return None

    @contextmanager
    def string_fixture() -> Iterator[str]:
        yield "value"

    asynchronous_suite = DriverConformanceSuite(
        "async-driver",
        (ConformanceCase("async-case", asynchronous),),
    )
    with pytest.raises(TypeError, match="synchronous"):
        asynchronous_suite.run(string_fixture)
