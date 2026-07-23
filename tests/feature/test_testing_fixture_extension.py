from __future__ import annotations

import pytest

import tests.fixtures.conformance_extension as fixture
from af.testing import (
    ConformanceEvidence,
    IntegrationUnavailable,
    TestApplicationBuilder,
)


def test_external_fixture_package_runs_provider_and_driver_conformance() -> None:
    fixture.reset_fixture_state()

    provider_report = fixture.PROVIDER_SUITE.run(
        fixture.provider_factory,
        builder_factory=TestApplicationBuilder,
    )
    driver_report = fixture.DRIVER_SUITE.run(fixture.driver_fixture)

    assert provider_report.evidence is ConformanceEvidence.ISOLATED
    assert provider_report.cases == (
        "resolves-fresh-service",
        "isolates-fresh-service",
    )
    assert not provider_report.certifies_production
    assert len(fixture.CREATED_SERVICES) == 2
    assert fixture.OBSERVED_SERVICES == fixture.CREATED_SERVICES
    assert fixture.CREATED_SERVICES[0] is not fixture.CREATED_SERVICES[1]
    assert [service.values for service in fixture.CREATED_SERVICES] == [
        ["observed"],
        ["observed"],
    ]

    assert driver_report.evidence is ConformanceEvidence.FAKE
    assert driver_report.cases == ("writes-and-reads", "starts-empty")
    assert not driver_report.certifies_production
    assert fixture.DRIVER_EVENTS == [
        ("open", 1),
        ("close", 1),
        ("open", 2),
        ("close", 2),
    ]


def test_external_fixture_package_keeps_real_services_explicitly_disabled() -> None:
    fixture.reset_fixture_state()

    with pytest.raises(IntegrationUnavailable, match="not configured"):
        fixture.DRIVER_SUITE.run_real(fixture.DISABLED_REAL_SERVICE)

    assert fixture.DRIVER_EVENTS == []
