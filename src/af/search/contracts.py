"""Driver-neutral, provenance-bearing search contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from types import MappingProxyType

__all__ = (
    "ResourceEnvelope",
    "ResourceKind",
    "SearchFilter",
    "SearchHit",
    "SearchQuery",
)

_IDENTIFIER = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.:-]{0,254}[A-Za-z0-9])?$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_TEXT = 1_048_576
_MAX_ATTRIBUTES = 64
_MAX_FILTERS = 64
_MAX_VALUES = 256


class ResourceKind(StrEnum):
    """Canonical source families accepted by the shared search API."""

    MARKDOWN = "markdown"
    RELATIONAL = "relational"
    BSON = "bson"
    GRAPH = "graph"
    CACHE = "cache"
    BLOB = "blob"
    EVENT = "event"
    TIME_SERIES = "time-series"
    TEXT = "text"
    VECTOR = "vector"


def _bounded(value: object, field_name: str, *, limit: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > limit
        or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value)
    ):
        raise ValueError(f"search {field_name} must be bounded non-empty text")
    return value


def _identifier(value: object, field_name: str) -> str:
    text = _bounded(value, field_name)
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"search {field_name} must be canonical")
    return text


def _evidence(
    values: object, field_name: str, *, digest: bool = False
) -> tuple[str, ...]:
    if type(values) is not tuple or not values or len(values) > _MAX_VALUES:
        raise ValueError(f"search {field_name} evidence is invalid")
    checked: list[str] = []
    for value in values:
        text = _bounded(value, field_name)
        if digest and _SHA256.fullmatch(text) is None:
            raise ValueError(f"search {field_name} digest is invalid")
        checked.append(text)
    if len(set(checked)) != len(checked):
        raise ValueError(f"search {field_name} evidence must be unique")
    return tuple(checked)


@dataclass(frozen=True, slots=True, repr=False)
class ResourceEnvelope:
    """One canonical search projection with authority and access evidence."""

    resource_id: str
    kind: ResourceKind
    source_id: str
    authoritative_locator: str
    tenant_id: str
    projection_version: int
    acl: tuple[str, ...]
    provenance: tuple[str, ...]
    text: str
    attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "resource_id", _identifier(self.resource_id, "resource id")
        )
        if not isinstance(self.kind, ResourceKind):
            raise ValueError("search resource kind is invalid")
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source id"))
        object.__setattr__(
            self,
            "authoritative_locator",
            _bounded(self.authoritative_locator, "authoritative locator", limit=2_048),
        )
        object.__setattr__(self, "tenant_id", _identifier(self.tenant_id, "tenant id"))
        if type(self.projection_version) is not int or self.projection_version < 1:
            raise ValueError("search projection version must be positive")
        object.__setattr__(self, "acl", _evidence(self.acl, "ACL"))
        object.__setattr__(
            self,
            "provenance",
            _evidence(self.provenance, "provenance", digest=True),
        )
        object.__setattr__(
            self, "text", _bounded(self.text, "resource text", limit=_MAX_TEXT)
        )
        if (
            not isinstance(self.attributes, Mapping)
            or len(self.attributes) > _MAX_ATTRIBUTES
        ):
            raise ValueError("search resource attributes are invalid")
        snapshot: dict[str, str] = {}
        for key, value in self.attributes.items():
            if type(key) is not str or _FIELD.fullmatch(key) is None:
                raise ValueError("search attribute name is invalid")
            snapshot[key] = _bounded(value, "attribute value", limit=2_048)
        object.__setattr__(self, "attributes", MappingProxyType(snapshot))

    def __repr__(self) -> str:
        return (
            f"ResourceEnvelope(resource_id={self.resource_id!r}, "
            f"kind={self.kind.value!r}, "
            f"source_id={self.source_id!r}, "
            f"projection_version={self.projection_version!r}, "
            "content='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True)
class SearchFilter:
    """One allowlisted exact-match predicate pushed down before ranking."""

    field: str
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.field) is not str or _FIELD.fullmatch(self.field) is None:
            raise ValueError("search filter field is invalid")
        object.__setattr__(self, "values", _evidence(self.values, "filter"))


@dataclass(frozen=True, slots=True, repr=False)
class SearchQuery:
    """Bounded text query with mandatory pre-ranking policy predicates."""

    text: str
    filters: tuple[SearchFilter, ...]
    limit: int = 20
    deadline_ms: int = 1_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", _bounded(self.text, "query text", limit=8_192))
        if (
            type(self.filters) is not tuple
            or not self.filters
            or len(self.filters) > _MAX_FILTERS
            or any(not isinstance(item, SearchFilter) for item in self.filters)
            or len({item.field for item in self.filters}) != len(self.filters)
        ):
            raise ValueError("search filters must be non-empty, bounded, and unique")
        if type(self.limit) is not int or not 1 <= self.limit <= 1_000:
            raise ValueError("search result limit is invalid")
        if type(self.deadline_ms) is not int or not 1 <= self.deadline_ms <= 60_000:
            raise ValueError("search deadline is invalid")

    def required_filter(self, field_name: str) -> tuple[str, ...] | None:
        if type(field_name) is not str or _FIELD.fullmatch(field_name) is None:
            raise ValueError("search filter lookup field is invalid")
        for item in self.filters:
            if item.field == field_name:
                return item.values
        return None

    def __repr__(self) -> str:
        return (
            f"SearchQuery(filter_count={len(self.filters)!r}, limit={self.limit!r}, "
            f"deadline_ms={self.deadline_ms!r}, text='[REDACTED]')"
        )


@dataclass(frozen=True, slots=True, repr=False)
class SearchHit:
    """Ranked result whose identity, authority, ACL, and provenance are explicit."""

    resource_id: str
    source_id: str
    authoritative_locator: str
    projection_version: int
    acl_evidence: tuple[str, ...]
    provenance: tuple[str, ...]
    score: float
    rank: int
    highlights: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "resource_id", _identifier(self.resource_id, "resource id")
        )
        object.__setattr__(self, "source_id", _identifier(self.source_id, "source id"))
        object.__setattr__(
            self,
            "authoritative_locator",
            _bounded(self.authoritative_locator, "authoritative locator", limit=2_048),
        )
        if type(self.projection_version) is not int or self.projection_version < 1:
            raise ValueError("search projection version must be positive")
        object.__setattr__(self, "acl_evidence", _evidence(self.acl_evidence, "ACL"))
        object.__setattr__(
            self,
            "provenance",
            _evidence(self.provenance, "provenance", digest=True),
        )
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ValueError("search score is invalid")
        score = float(self.score)
        if not isfinite(score) or score < 0:
            raise ValueError("search score is invalid")
        object.__setattr__(self, "score", score)
        if type(self.rank) is not int or not 1 <= self.rank <= 1_000:
            raise ValueError("search rank is invalid")
        if type(self.highlights) is not tuple or len(self.highlights) > 64:
            raise ValueError("search highlights are invalid")
        object.__setattr__(
            self,
            "highlights",
            tuple(
                _bounded(value, "highlight", limit=8_192) for value in self.highlights
            ),
        )

    def __repr__(self) -> str:
        return (
            f"SearchHit(resource_id={self.resource_id!r}, "
            f"source_id={self.source_id!r}, "
            f"projection_version={self.projection_version!r}, score={self.score!r}, "
            f"rank={self.rank!r}, evidence='[REDACTED]')"
        )
