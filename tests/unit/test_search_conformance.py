from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from af.search.embedded import EmbeddedSearchDriver
from af.search.manager import SearchDriver
from af.testing import ConformanceEvidence, SearchDriverConformanceSuite


def test_embedded_driver_passes_shared_search_conformance() -> None:
    @contextmanager
    def fixture() -> Iterator[SearchDriver]:
        with EmbeddedSearchDriver(":memory:") as driver:
            yield driver

    report = SearchDriverConformanceSuite().run(fixture)

    assert report.evidence is ConformanceEvidence.FAKE
    assert report.suite == "search-driver"
    assert {
        "canonical-sources",
        "policy-pushdown",
        "replacement-and-tombstone",
        "deterministic-order",
        "error-redaction",
        "shutdown",
    } == set(report.cases)
