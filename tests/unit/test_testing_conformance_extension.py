from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from af.testing import (
    ConformanceCase,
    ConformanceEvidence,
    DriverConformanceSuite,
    ProcessStateLeak,
    TestApplicationBuilder,
)
from tests.fixtures.conformance_extension import (
    DRIVER_SUITE,
    PROVIDER_SUITE,
    FixtureProvider,
    driver_fixture,
)


def test_external_style_fixture_package_runs_provider_and_driver_suites() -> None:
    provider = PROVIDER_SUITE.run(
        FixtureProvider,
        builder_factory=TestApplicationBuilder,
    )
    driver = DRIVER_SUITE.run(driver_fixture)

    assert provider.cases == ("resolves-service",)
    assert provider.evidence is ConformanceEvidence.ISOLATED
    assert provider.certifies_production is False
    assert driver.cases == ("exercises-driver",)
    assert driver.evidence is ConformanceEvidence.FAKE
    assert driver.certifies_production is False


def test_conformance_suite_rejects_and_restores_process_global_mutation() -> None:
    key = "AF_TEST_CONFORMANCE_PROCESS_LEAK"
    previous = os.environ.pop(key, None)

    @contextmanager
    def fixture() -> Iterator[object]:
        yield object()

    def leak_environment(_driver: object) -> None:
        os.environ[key] = "sensitive-value"

    suite = DriverConformanceSuite(
        "leaking-driver",
        (ConformanceCase("leaks-environment", leak_environment),),
    )
    try:
        with pytest.raises(ProcessStateLeak) as captured:
            suite.run(fixture)
        assert captured.value.surfaces == ("environment",)
        assert key not in os.environ
        assert "sensitive-value" not in str(captured.value)
    finally:
        if previous is not None:
            os.environ[key] = previous
