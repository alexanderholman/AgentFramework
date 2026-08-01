from __future__ import annotations

from collections.abc import Iterator
from threading import Event, Lock, current_thread
from time import monotonic

import pytest

from af.framework.errors import PlatformError, PlatformReason
from af.search.contracts import SearchFilter, SearchHit, SearchQuery
from af.search.federation import (
    FederatedSearchResult,
    FederationContribution,
    FederationExplanation,
    FederationFailure,
    FederationIntegrityError,
    SearchFederator,
)


def _query(*, limit: int = 20, deadline_ms: int = 1_000) -> SearchQuery:
    return SearchQuery(
        text="energy",
        filters=(
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
        ),
        limit=limit,
        deadline_ms=deadline_ms,
    )


def _hit(
    resource_id: str,
    *,
    rank: int,
    locator: str | None = None,
    provenance: str = "a",
    highlights: tuple[str, ...] = (),
    tenant_id: str = "tenant-a",
    acl_evidence: tuple[str, ...] = ("group:research",),
) -> SearchHit:
    return SearchHit(
        resource_id=resource_id,
        source_id="source-primary",
        authoritative_locator=locator or f"af://source-primary/{resource_id}",
        tenant_id=tenant_id,
        projection_version=1,
        acl_evidence=acl_evidence,
        provenance=("sha256:" + provenance * 64,),
        score=1.0 / rank,
        rank=rank,
        highlights=highlights,
    )


class Router:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.calls: list[tuple[str | None, SearchQuery]] = []

    def search(
        self,
        query: SearchQuery,
        *,
        name: str | None = None,
    ) -> tuple[SearchHit, ...]:
        self.calls.append((name, query))
        result = self.results[name or ""]
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


def _clock(*values: float) -> Iterator[float]:
    yield from values
    while True:
        yield values[-1]


def test_federator_deduplicates_with_rrf_and_deterministic_explanations() -> None:
    router = Router(
        {
            "alpha": (
                _hit("shared", rank=1, highlights=("alpha excerpt",)),
                _hit("alpha-only", rank=2),
            ),
            "beta": (
                _hit("beta-only", rank=1),
                _hit("shared", rank=2, highlights=("beta excerpt",)),
            ),
        }
    )

    result = SearchFederator(router, sources=("alpha", "beta")).search(_query())

    assert [hit.resource_id for hit in result.hits] == [
        "shared",
        "beta-only",
        "alpha-only",
    ]
    assert result.hits[0].rank == 1
    assert result.hits[0].highlights == ("alpha excerpt", "beta excerpt")
    assert [item.source_name for item in result.explanations[0].contributions] == [
        "alpha",
        "beta",
    ]
    assert result.searched_sources == ("alpha", "beta")
    assert result.failures == ()
    assert result.complete


def test_federator_preserves_policy_filters_and_shrinks_absolute_deadline() -> None:
    router = Router({"alpha": (), "beta": ()})

    class DeterministicClock:
        def __init__(self) -> None:
            self._main_values = iter((10.0, 10.1, 10.2, 10.3, 10.4))

        def __call__(self) -> float:
            if current_thread().name.startswith("af-search-federation-"):
                return 10.15
            return next(self._main_values)

    federator = SearchFederator(
        router,
        sources=("alpha", "beta"),
        clock=DeterministicClock(),
    )

    result = federator.search(_query(deadline_ms=1_000))

    assert result.complete
    assert [call[1].deadline_ms for call in router.calls] == [900, 800]
    assert all(
        call[1].filters
        == (
            SearchFilter("tenant_id", ("tenant-a",)),
            SearchFilter("acl", ("group:research",)),
        )
        for call in router.calls
    )


def test_federator_reports_sanitized_partial_failures_and_keeps_safe_hits() -> None:
    router = Router(
        {
            "unsupported": PlatformError(
                PlatformReason.CAPABILITY_UNAVAILABLE,
                stage="search.connection",
                protected_cause=RuntimeError("secret backend detail"),
            ),
            "broken": RuntimeError("credential=secret"),
            "healthy": (_hit("resource-a", rank=1),),
        }
    )

    result = SearchFederator(
        router,
        sources=("unsupported", "broken", "healthy"),
    ).search(_query())

    assert [hit.resource_id for hit in result.hits] == ["resource-a"]
    assert [(item.source_name, item.reason) for item in result.failures] == [
        ("unsupported", PlatformReason.CAPABILITY_UNAVAILABLE),
        ("broken", PlatformReason.PERMANENT_FAILURE),
    ]
    assert not result.complete
    assert "secret" not in repr(result)


def test_federator_returns_fast_hits_while_stalled_sources_time_out() -> None:
    slow_started = Event()
    release_slow = Event()

    class BlockingRouter:
        def __init__(self) -> None:
            self.calls: list[tuple[str | None, SearchQuery]] = []

        def search(
            self,
            query: SearchQuery,
            *,
            name: str | None = None,
        ) -> tuple[SearchHit, ...]:
            if name == "slow":
                slow_started.set()
                self.calls.append((name, query))
                release_slow.wait(0.2)
                return (_hit("late", rank=1),)
            assert slow_started.wait(0.05)
            self.calls.append((name, query))
            return (_hit("fast", rank=1),)

    router = BlockingRouter()
    started_at = monotonic()
    try:
        result = SearchFederator(router, sources=("slow", "fast")).search(_query(deadline_ms=50))
    finally:
        release_slow.set()

    assert monotonic() - started_at < 0.15
    assert [name for name, _query_value in router.calls] == ["slow", "fast"]
    assert [hit.resource_id for hit in result.hits] == ["fast"]
    assert result.searched_sources == ("fast",)
    assert [(item.source_name, item.reason) for item in result.failures] == [
        ("slow", PlatformReason.CANCELLED),
    ]


def test_federator_keeps_timely_queued_outcomes_after_deadline_expires() -> None:
    class CoordinatedRouter:
        def __init__(self) -> None:
            self._started = 0
            self._lock = Lock()
            self._release = Event()

        def search(
            self,
            query: SearchQuery,
            *,
            name: str | None = None,
        ) -> tuple[SearchHit, ...]:
            assert name is not None
            with self._lock:
                self._started += 1
                if self._started == 2:
                    self._release.set()
            assert self._release.wait(0.1)
            return (_hit(name, rank=1),)

    class DeterministicClock:
        def __init__(self) -> None:
            self._values = iter((10.0, 10.01, 10.02, 10.03, 10.04, 10.11))

        def __call__(self) -> float:
            if current_thread().name.startswith("af-search-federation-"):
                return 10.05
            return next(self._values)

    result = SearchFederator(
        CoordinatedRouter(),
        sources=("alpha", "beta"),
        clock=DeterministicClock(),
    ).search(_query(deadline_ms=100))

    assert [hit.resource_id for hit in result.hits] == ["alpha", "beta"]
    assert result.searched_sources == ("alpha", "beta")
    assert result.failures == ()


@pytest.mark.parametrize(
    "hits",
    [
        [_hit("resource-a", rank=1)],
        (_hit("resource-a", rank=1), _hit("resource-a", rank=2)),
        (object(),),
    ],
)
def test_federator_rejects_malformed_source_results(hits: object) -> None:
    with pytest.raises(FederationIntegrityError) as captured:
        SearchFederator(Router({"alpha": hits}), sources=("alpha",)).search(_query())
    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE


@pytest.mark.parametrize(
    "hits",
    [
        (_hit("resource-a", rank=1), _hit("resource-b", rank=1)),
        (_hit("resource-a", rank=2), _hit("resource-b", rank=1)),
        tuple(_hit(f"resource-{index}", rank=index + 1) for index in range(21)),
    ],
)
def test_federator_rejects_noncanonical_or_unbounded_source_ranking(
    hits: tuple[SearchHit, ...],
) -> None:
    with pytest.raises(FederationIntegrityError) as captured:
        SearchFederator(Router({"alpha": hits}), sources=("alpha",)).search(_query())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE


def test_federator_fails_closed_on_contradictory_authority_evidence() -> None:
    router = Router(
        {
            "alpha": (_hit("shared", rank=1),),
            "beta": (
                _hit(
                    "shared",
                    rank=1,
                    locator="af://conflicting/shared",
                    provenance="b",
                ),
            ),
        }
    )

    with pytest.raises(FederationIntegrityError) as captured:
        SearchFederator(router, sources=("alpha", "beta")).search(_query())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert "conflicting" not in str(captured.value)


@pytest.mark.parametrize(
    ("tenant_id", "acl_evidence"),
    [
        ("tenant-b", ("group:research",)),
        ("tenant-a", ("group:other",)),
    ],
)
def test_federator_fails_closed_when_returned_hits_violate_query_policy(
    tenant_id: str,
    acl_evidence: tuple[str, ...],
) -> None:
    router = Router(
        {
            "alpha": (
                _hit(
                    "unauthorized",
                    rank=1,
                    tenant_id=tenant_id,
                    acl_evidence=acl_evidence,
                ),
            )
        }
    )

    with pytest.raises(FederationIntegrityError) as captured:
        SearchFederator(router, sources=("alpha",)).search(_query())

    assert captured.value.reason is PlatformReason.INTEGRITY_FAILURE
    assert "unauthorized" not in str(captured.value)


def test_federator_limits_results_and_orders_ties_by_stable_identity() -> None:
    router = Router(
        {
            "alpha": (_hit("resource-b", rank=1),),
            "beta": (_hit("resource-a", rank=1),),
        }
    )

    result = SearchFederator(router, sources=("alpha", "beta")).search(_query(limit=1))

    assert [hit.resource_id for hit in result.hits] == ["resource-a"]


@pytest.mark.parametrize(
    ("sources", "rrf_constant"),
    [
        ((), 60),
        (("alpha", "alpha"), 60),
        (("bad source",), 60),
        (tuple(f"source-{index}" for index in range(17)), 60),
        (("alpha",), 0),
    ],
)
def test_federator_rejects_invalid_bounds(
    sources: tuple[str, ...],
    rrf_constant: int,
) -> None:
    with pytest.raises(ValueError):
        SearchFederator(
            Router({"alpha": ()}),
            sources=sources,
            rrf_constant=rrf_constant,
        )


def test_federator_rejects_invalid_router_clock_and_query() -> None:
    with pytest.raises(ValueError):
        SearchFederator(object(), sources=("alpha",))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SearchFederator(
            Router({"alpha": ()}),
            sources=("alpha",),
            clock=None,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        SearchFederator(Router({"alpha": ()}), sources=("alpha",)).search(
            object()  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant_id", "bad tenant"),
        ("source_id", "bad/source"),
        ("resource_id", ""),
    ],
)
def test_federation_explanation_rejects_noncanonical_identity(
    field: str,
    value: str,
) -> None:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "source_id": "source-primary",
        "resource_id": "resource-a",
        "contributions": (FederationContribution("alpha", 1, 0.5),),
    }
    values[field] = value

    with pytest.raises(ValueError, match="must be canonical"):
        FederationExplanation(**values)  # type: ignore[arg-type]


def test_federation_value_contracts_reject_invalid_evidence() -> None:
    hit = _hit("resource-a", rank=1)
    contribution = FederationContribution("alpha", 1, 0.5)
    explanation = FederationExplanation(
        tenant_id=hit.tenant_id,
        source_id=hit.source_id,
        resource_id=hit.resource_id,
        contributions=(contribution,),
    )

    with pytest.raises(ValueError):
        FederationFailure("alpha", "invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FederationContribution("alpha", 0, 0.5)
    with pytest.raises(ValueError):
        FederationContribution("alpha", 1, float("nan"))
    with pytest.raises(ValueError):
        FederationExplanation("tenant-a", "source-primary", "resource-a", ())
    with pytest.raises(ValueError):
        FederatedSearchResult(
            hits=(hit,),
            explanations=(),
            searched_sources=("alpha",),
            failures=(),
        )
    with pytest.raises(ValueError):
        FederatedSearchResult(
            hits=(hit,),
            explanations=(explanation,),
            searched_sources=("alpha",),
            failures=(object(),),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        FederatedSearchResult(
            hits=(hit,),
            explanations=(
                FederationExplanation(
                    tenant_id=hit.tenant_id,
                    source_id=hit.source_id,
                    resource_id="different",
                    contributions=(contribution,),
                ),
            ),
            searched_sources=("alpha",),
            failures=(),
        )


def test_federated_result_rejects_contradictory_source_and_identity_evidence() -> None:
    hit = _hit("resource-a", rank=1)
    alpha = FederationContribution("alpha", 1, 0.5)
    explanation = FederationExplanation(
        tenant_id=hit.tenant_id,
        source_id=hit.source_id,
        resource_id=hit.resource_id,
        contributions=(alpha,),
    )

    with pytest.raises(ValueError, match="hit identities"):
        FederatedSearchResult(
            hits=(hit, hit),
            explanations=(explanation, explanation),
            searched_sources=("alpha",),
            failures=(),
        )
    with pytest.raises(ValueError, match="failure sources"):
        FederatedSearchResult(
            hits=(),
            explanations=(),
            searched_sources=(),
            failures=(
                FederationFailure("alpha", PlatformReason.CANCELLED),
                FederationFailure("alpha", PlatformReason.PERMANENT_FAILURE),
            ),
        )
    with pytest.raises(ValueError, match="source outcomes"):
        FederatedSearchResult(
            hits=(),
            explanations=(),
            searched_sources=("alpha",),
            failures=(FederationFailure("alpha", PlatformReason.CANCELLED),),
        )
    with pytest.raises(ValueError, match="source outcomes exceed"):
        FederatedSearchResult(
            hits=(),
            explanations=(),
            searched_sources=tuple(f"source-{index}" for index in range(16)),
            failures=(FederationFailure("overflow", PlatformReason.PERMANENT_FAILURE),),
        )
    with pytest.raises(ValueError, match="contribution sources"):
        FederatedSearchResult(
            hits=(hit,),
            explanations=(
                FederationExplanation(
                    tenant_id=hit.tenant_id,
                    source_id=hit.source_id,
                    resource_id=hit.resource_id,
                    contributions=(FederationContribution("beta", 1, 0.5),),
                ),
            ),
            searched_sources=("alpha",),
            failures=(),
        )
