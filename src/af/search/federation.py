"""Bounded, policy-preserving search federation."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from math import isfinite
from queue import Empty, Queue
from threading import Thread
from time import monotonic
from typing import Protocol

from af.framework.errors import PlatformError, PlatformReason
from af.search.contracts import SearchHit, SearchQuery

__all__ = (
    "FederatedSearchResult",
    "FederationContribution",
    "FederationExplanation",
    "FederationFailure",
    "FederationIntegrityError",
    "SearchFederator",
)

_SOURCE_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,254}[A-Za-z0-9])?$")
_MAX_SOURCES = 16
_DEFAULT_RRF_CONSTANT = 60


class _SearchRouter(Protocol):
    def search(
        self,
        query: SearchQuery,
        *,
        name: str | None = None,
    ) -> tuple[SearchHit, ...]: ...


class FederationIntegrityError(PlatformError):
    """Sanitized failure for contradictory or malformed search evidence."""

    def __init__(self, *, protected_cause: BaseException) -> None:
        super().__init__(
            PlatformReason.INTEGRITY_FAILURE,
            stage="search.federation",
            protected_cause=protected_cause,
        )


@dataclass(frozen=True, slots=True)
class FederationFailure:
    """One source that could not safely contribute to a federated result."""

    source_name: str
    reason: PlatformReason

    def __post_init__(self) -> None:
        _validate_source_name(self.source_name)
        if not isinstance(self.reason, PlatformReason):
            raise ValueError("federation failure reason is invalid")


@dataclass(frozen=True, slots=True)
class FederationContribution:
    """Bounded ranking evidence from one named search source."""

    source_name: str
    source_rank: int
    reciprocal_rank_score: float

    def __post_init__(self) -> None:
        _validate_source_name(self.source_name)
        if type(self.source_rank) is not int or not 1 <= self.source_rank <= 1_000:
            raise ValueError("federation source rank is invalid")
        if (
            isinstance(self.reciprocal_rank_score, bool)
            or not isinstance(self.reciprocal_rank_score, (int, float))
            or not isfinite(float(self.reciprocal_rank_score))
            or self.reciprocal_rank_score <= 0
        ):
            raise ValueError("federation contribution score is invalid")
        object.__setattr__(
            self,
            "reciprocal_rank_score",
            float(self.reciprocal_rank_score),
        )


@dataclass(frozen=True, slots=True)
class FederationExplanation:
    """Deterministic source contributions for one returned resource."""

    tenant_id: str
    source_id: str
    resource_id: str
    contributions: tuple[FederationContribution, ...]

    def __post_init__(self) -> None:
        _validate_identity(self.tenant_id, "tenant identity")
        _validate_identity(self.source_id, "source identity")
        _validate_identity(self.resource_id, "resource identity")
        if (
            type(self.contributions) is not tuple
            or not self.contributions
            or len(self.contributions) > _MAX_SOURCES
            or any(
                not isinstance(contribution, FederationContribution)
                for contribution in self.contributions
            )
            or len({item.source_name for item in self.contributions}) != len(self.contributions)
        ):
            raise ValueError("federation explanation is invalid")


@dataclass(frozen=True, slots=True)
class FederatedSearchResult:
    """Bounded hits plus explicit success and partial-failure evidence."""

    hits: tuple[SearchHit, ...]
    explanations: tuple[FederationExplanation, ...]
    searched_sources: tuple[str, ...]
    failures: tuple[FederationFailure, ...]

    def __post_init__(self) -> None:
        if (
            type(self.hits) is not tuple
            or any(not isinstance(hit, SearchHit) for hit in self.hits)
            or len(self.hits) > 1_000
        ):
            raise ValueError("federated hits are invalid")
        if (
            type(self.explanations) is not tuple
            or len(self.explanations) != len(self.hits)
            or any(
                not isinstance(explanation, FederationExplanation)
                for explanation in self.explanations
            )
        ):
            raise ValueError("federation explanations are invalid")
        searched_sources = frozenset(_validate_sources(self.searched_sources, allow_empty=True))
        if (
            type(self.failures) is not tuple
            or len(self.failures) > _MAX_SOURCES
            or any(not isinstance(failure, FederationFailure) for failure in self.failures)
        ):
            raise ValueError("federation failures are invalid")
        failure_sources = tuple(failure.source_name for failure in self.failures)
        if len(set(failure_sources)) != len(failure_sources):
            raise ValueError("federation failure sources must be unique")
        if len(searched_sources) + len(failure_sources) > _MAX_SOURCES:
            raise ValueError("federation source outcomes exceed the configured bound")
        if searched_sources.intersection(failure_sources):
            raise ValueError("federation source outcomes must be disjoint")
        hit_identities = tuple(_identity(hit) for hit in self.hits)
        explanation_identities = tuple(
            (item.tenant_id, item.source_id, item.resource_id) for item in self.explanations
        )
        if len(set(hit_identities)) != len(hit_identities):
            raise ValueError("federated hit identities must be unique")
        if hit_identities != explanation_identities:
            raise ValueError("federation explanations do not match hits")
        if any(
            contribution.source_name not in searched_sources
            for explanation in self.explanations
            for contribution in explanation.contributions
        ):
            raise ValueError("federation contribution sources were not searched")

    @property
    def complete(self) -> bool:
        """Whether every configured source completed inside the deadline."""

        return not self.failures


@dataclass(slots=True)
class _Candidate:
    hit: SearchHit
    score: float
    contributions: list[FederationContribution]
    highlights: list[str]


@dataclass(slots=True)
class _SourceOutcome:
    source_name: str
    query: SearchQuery
    completed_at: float
    hits: object | None = None
    failure_reason: PlatformReason | None = None


def _validate_identity(value: object, field_name: str) -> str:
    if type(value) is not str or _SOURCE_NAME.fullmatch(value) is None:
        raise ValueError(f"federation {field_name} must be canonical")
    return value


def _validate_source_name(value: object) -> str:
    return _validate_identity(value, "source name")


def _validate_sources(
    values: object,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(values) is not tuple or (not values and not allow_empty) or len(values) > _MAX_SOURCES:
        raise ValueError("federation sources must be a bounded tuple")
    checked = tuple(_validate_source_name(value) for value in values)
    if len(set(checked)) != len(checked):
        raise ValueError("federation sources must be unique")
    return checked


def _identity(hit: SearchHit) -> tuple[str, str, str]:
    return (hit.tenant_id, hit.source_id, hit.resource_id)


def _same_authority(left: SearchHit, right: SearchHit) -> bool:
    return (
        left.authoritative_locator == right.authoritative_locator
        and left.projection_version == right.projection_version
        and left.acl_evidence == right.acl_evidence
        and left.provenance == right.provenance
    )


class SearchFederator:
    """Query bounded named sources concurrently and fuse trustworthy results."""

    def __init__(
        self,
        manager: _SearchRouter,
        *,
        sources: tuple[str, ...],
        rrf_constant: int = _DEFAULT_RRF_CONSTANT,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if not hasattr(manager, "search") or not callable(manager.search):
            raise ValueError("federation manager must provide search routing")
        self._manager = manager
        self._sources = _validate_sources(sources)
        if type(rrf_constant) is not int or not 1 <= rrf_constant <= 1_000:
            raise ValueError("federation RRF constant is invalid")
        if not callable(clock):
            raise ValueError("federation clock must be callable")
        self._rrf_constant = rrf_constant
        self._clock = clock

    def search(self, query: SearchQuery) -> FederatedSearchResult:
        """Search sources inside one absolute deadline and report partial failures."""

        if not isinstance(query, SearchQuery):
            raise ValueError("federation query is invalid")
        deadline = self._clock() + (query.deadline_ms / 1_000)
        candidates: dict[tuple[str, str, str], _Candidate] = {}
        outcomes: Queue[_SourceOutcome] = Queue()
        launched: dict[str, SearchQuery] = {}
        received: dict[str, _SourceOutcome] = {}
        pending: set[str] = set()
        searched: list[str] = []
        failures: list[FederationFailure] = []

        for position, source_name in enumerate(self._sources):
            remaining_ms = int((deadline - self._clock()) * 1_000)
            if remaining_ms < 1:
                failures.extend(
                    FederationFailure(name, PlatformReason.CANCELLED)
                    for name in self._sources[position:]
                )
                break
            source_query = replace(
                query,
                deadline_ms=min(query.deadline_ms, remaining_ms),
            )
            launched[source_name] = source_query
            pending.add(source_name)
            Thread(
                target=self._search_source,
                args=(source_name, source_query, outcomes),
                daemon=True,
                name=f"af-search-federation-{source_name}",
            ).start()

        while pending:
            remaining = deadline - self._clock()
            if remaining <= 0:
                break
            try:
                outcome = outcomes.get(timeout=remaining)
            except Empty:
                break
            if outcome.source_name not in pending:
                continue
            pending.remove(outcome.source_name)
            if outcome.completed_at > deadline:
                failures.append(FederationFailure(outcome.source_name, PlatformReason.CANCELLED))
                continue
            received[outcome.source_name] = outcome

        for source_name in self._sources:
            if source_name not in received:
                if source_name in launched and source_name not in {
                    failure.source_name for failure in failures
                }:
                    failures.append(FederationFailure(source_name, PlatformReason.CANCELLED))
                continue
            outcome = received[source_name]
            if outcome.failure_reason is not None:
                failures.append(FederationFailure(source_name, outcome.failure_reason))
                continue
            searched.append(source_name)
            self._accumulate(
                candidates,
                source_name,
                outcome.hits,
                query=outcome.query,
            )

        ordered = sorted(
            candidates.values(),
            key=lambda item: (-item.score, _identity(item.hit)),
        )[: query.limit]
        ranked_hits: list[SearchHit] = []
        explanations: list[FederationExplanation] = []
        for rank, candidate in enumerate(ordered, start=1):
            contributions = tuple(
                sorted(candidate.contributions, key=lambda item: item.source_name)
            )
            ranked_hits.append(
                replace(
                    candidate.hit,
                    score=candidate.score,
                    rank=rank,
                    highlights=tuple(candidate.highlights),
                )
            )
            explanations.append(
                FederationExplanation(
                    tenant_id=candidate.hit.tenant_id,
                    source_id=candidate.hit.source_id,
                    resource_id=candidate.hit.resource_id,
                    contributions=contributions,
                )
            )
        return FederatedSearchResult(
            hits=tuple(ranked_hits),
            explanations=tuple(explanations),
            searched_sources=tuple(searched),
            failures=tuple(failures),
        )

    def _search_source(
        self,
        source_name: str,
        source_query: SearchQuery,
        outcomes: Queue[_SourceOutcome],
    ) -> None:
        try:
            hits = self._manager.search(source_query, name=source_name)
        except PlatformError as error:
            outcomes.put(
                _SourceOutcome(
                    source_name=source_name,
                    query=source_query,
                    completed_at=self._clock(),
                    failure_reason=error.reason,
                )
            )
            return
        except Exception:
            outcomes.put(
                _SourceOutcome(
                    source_name=source_name,
                    query=source_query,
                    completed_at=self._clock(),
                    failure_reason=PlatformReason.PERMANENT_FAILURE,
                )
            )
            return
        outcomes.put(
            _SourceOutcome(
                source_name=source_name,
                query=source_query,
                completed_at=self._clock(),
                hits=hits,
            )
        )

    def _accumulate(
        self,
        candidates: dict[tuple[str, str, str], _Candidate],
        source_name: str,
        hits: object,
        *,
        query: SearchQuery,
    ) -> None:
        if (
            type(hits) is not tuple
            or len(hits) > query.limit
            or any(not isinstance(hit, SearchHit) for hit in hits)
        ):
            raise FederationIntegrityError(
                protected_cause=ValueError("search source returned malformed hits")
            )
        if tuple(hit.rank for hit in hits) != tuple(range(1, len(hits) + 1)):
            raise FederationIntegrityError(
                protected_cause=ValueError("search source returned noncanonical rank evidence")
            )
        tenant_filter = query.required_filter("tenant_id")
        acl_filter = query.required_filter("acl")
        if tenant_filter is None or acl_filter is None:
            raise FederationIntegrityError(
                protected_cause=ValueError("search query policy evidence is unavailable")
            )
        allowed_tenants = frozenset(tenant_filter)
        allowed_acl = frozenset(acl_filter)
        seen: set[tuple[str, str, str]] = set()
        for hit in hits:
            if hit.tenant_id not in allowed_tenants or allowed_acl.isdisjoint(hit.acl_evidence):
                raise FederationIntegrityError(
                    protected_cause=ValueError("search source returned a policy-ineligible hit")
                )
            identity = _identity(hit)
            if identity in seen:
                raise FederationIntegrityError(
                    protected_cause=ValueError("search source returned duplicate resource identity")
                )
            seen.add(identity)
            contribution_score = 1.0 / (self._rrf_constant + hit.rank)
            contribution = FederationContribution(
                source_name=source_name,
                source_rank=hit.rank,
                reciprocal_rank_score=contribution_score,
            )
            candidate = candidates.get(identity)
            if candidate is None:
                candidates[identity] = _Candidate(
                    hit=hit,
                    score=contribution_score,
                    contributions=[contribution],
                    highlights=list(hit.highlights),
                )
                continue
            if not _same_authority(candidate.hit, hit):
                raise FederationIntegrityError(
                    protected_cause=ValueError(
                        "search sources returned contradictory authority evidence"
                    )
                )
            candidate.score += contribution_score
            candidate.contributions.append(contribution)
            for highlight in hit.highlights:
                if highlight not in candidate.highlights and len(candidate.highlights) < 64:
                    candidate.highlights.append(highlight)
