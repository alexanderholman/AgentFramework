from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest

from af.framework.relational import DatabaseDriver, DatabaseFeature
from af.infrastructure.sqlite import SQLiteDriver
from af.testing import (
    ConformanceEvidence,
    RealServiceHook,
    RelationalDriverConformanceSuite,
    relational_driver_cases,
)


class _IncompleteDriver:
    @property
    def features(self) -> frozenset[DatabaseFeature]:
        return frozenset()


def test_sqlite_passes_shared_relational_real_service_suite() -> None:
    @contextmanager
    def fixture() -> Iterator[DatabaseDriver]:
        with SQLiteDriver(":memory:") as driver:
            yield driver

    hook: RealServiceHook[DatabaseDriver] = RealServiceHook(
        "sqlite",
        fixture,
        enabled=True,
    )
    report = RelationalDriverConformanceSuite().run_real(hook)

    assert report.evidence is ConformanceEvidence.REAL_SERVICE
    assert report.integration == "sqlite"
    assert report.cases == (
        "capabilities",
        "commit",
        "rollback",
        "savepoint",
        "stream-lifetime",
        "read-only",
        "error-redaction",
        "shutdown",
    )


def test_relational_cases_are_fresh_bounded_contract_values() -> None:
    first = relational_driver_cases()
    second = relational_driver_cases()

    assert first is not second
    assert tuple(case.name for case in first) == tuple(case.name for case in second)
    assert len(first) == 8


def test_relational_suite_rejects_incomplete_capability_evidence() -> None:
    capability_case = relational_driver_cases()[0]

    with pytest.raises(AssertionError, match="incomplete"):
        capability_case.exercise(cast(DatabaseDriver, _IncompleteDriver()))
