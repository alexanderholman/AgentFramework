from __future__ import annotations

import pytest

from af.testing import (
    ConformanceEvidence,
    IntegrationUnavailable,
    TestApplicationBuilder,
)
from tests.fixtures.conformance_extension import (
    CREATED_SERVICES,
    DISABLED_REAL_SERVICE,
    DRIVER_EVENTS,
    DRIVER_SUITE,
    OBSERVED_SERVICES,
    PROVIDER_SUITE,
    driver_fixture,
    provider_factory,
    reset_fixture_state,
)


def test_external_fixture_package_runs_provider_and_driver_conformance() -> None:
    reset_fixture_state()

    provider_report = PROVIDER_SUITE.run(
        provider_factory,
        builder_factory=TestApplicationBuilder,
    )
    driver_report = DRIVER_SUITE.run(driver_fixture)

    assert provider_report.evidence is ConformanceEvidence.ISOLATED
    assert provider_report.cases == (
        "resolves-fresh-service",
        "isolates-fresh-service",
    )
    assert not provider_report.certifies_production
    assert len(CREATED_SERVICES) == 2
    assert OBSERVED_SERVICES == CREATED_SERVICES
    assert CREATED_SERVICES[0] is not CREATED_SERVICES[1]
    assert [service.values for service in CREATED_SERVICES] == [
        ["observed"],
        ["observed"],
    ]

    assert driver_report.evidence is ConformanceEvidence.FAKE
    assert driver_report.cases == ("writes-and-reads", "starts-empty")
    assert not driver_report.certifies_production
    assert DRIVER_EVENTS == [
        ("open", 1),
        ("close", 1),
        ("open", 2),
        ("close", 2),
    ]


def test_external_fixture_package_keeps_real_services_explicitly_disabled() -> None:
    reset_fixture_state()

    with pytest.raises(IntegrationUnavailable, match="not configured"):
        DRIVER_SUITE.run_real(DISABLED_REAL_SERVICE)

    assert DRIVER_EVENTS == []
