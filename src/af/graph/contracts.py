from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _identifier(value: str, name: str, max_len: int = 64) -> str:
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > max_len:
        raise ValueError(f"{name} must be at most {max_len} characters")
    return value


def _bounded(value: int | float, name: str, lo: int | float, hi: int | float) -> int | float:
    if value < lo or value > hi:
        raise ValueError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _label(value: str) -> str:
    if not value:
        raise ValueError("label must not be empty")
    if len(value) > 64:
        raise ValueError("label must be at most 64 characters")
    return value


def _properties(props: Mapping[str, Any]) -> dict[str, Any]:
    for k, v in props.items():
        if isinstance(v, str) and len(v) > 100_000:
            raise ValueError(f"property value for key {k!r} exceeds 100000 characters")
    return dict(props)


# ---------------------------------------------------------------------------
# Parameter validation
# ---------------------------------------------------------------------------

_PARAMETER_PLACEHOLDER_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_UNTRUSTED_VALUE_RE = re.compile(
    r"(;|--|/\*|\*/|DROP|DELETE|INSERT|UPDATE|ALTER|CREATE|EXEC)", re.IGNORECASE
)


def _validate_parameter_value(value: Any) -> None:
    """Reject untrusted parameter values that look like injection attempts."""
    if isinstance(value, str) and _UNTRUSTED_VALUE_RE.search(value):
        raise ValueError(f"untrusted parameter value rejected: {value!r}")


def _validate_parameterized_query(text: str, parameters: Mapping[str, Any]) -> None:
    """Validate that parameterized queries use placeholders and bind parameters."""
    placeholders = _PARAMETER_PLACEHOLDER_RE.findall(text)
    placeholder_names = {ph[1:] for ph in placeholders}

    if placeholder_names and not parameters:
        raise ValueError("Query contains placeholders but no parameters provided")

    if not placeholder_names and parameters:
        raise ValueError(
            "Query contains no placeholders but parameters provided — "
            "possible interpolation attempt"
        )

    for _, value in parameters.items():
        _validate_parameter_value(value)

    if placeholder_names and set(parameters.keys()) != placeholder_names:
        raise ValueError(
            f"Parameter mismatch: query expects {placeholder_names}, got {set(parameters.keys())}"
        )


# ---------------------------------------------------------------------------
# Feature enum
# ---------------------------------------------------------------------------


class GraphFeature(StrEnum):
    QUERY = "query"
    INDEXING = "indexing"
    PERSISTENCE = "persistence"
    CANCELLATION = "cancellation"


# ---------------------------------------------------------------------------
# Portable graph data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphNode:
    """Portable representation of a graph node."""

    node_id: str
    labels: tuple[str, ...]
    properties: dict[str, Any]

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id")
        if not self.labels:
            raise ValueError("labels must not be empty")
        for lbl in self.labels:
            _label(lbl)
        _properties(self.properties)


@dataclass(frozen=True)
class GraphEdge:
    """Portable representation of a graph edge."""

    edge_id: str
    source_id: str
    target_id: str
    relation_type: str
    properties: dict[str, Any]

    def __post_init__(self) -> None:
        _identifier(self.edge_id, "edge_id")
        _identifier(self.source_id, "source_id")
        _identifier(self.target_id, "target_id")
        if self.source_id == self.target_id:
            raise ValueError("source_id and target_id must differ")
        _label(self.relation_type)
        _properties(self.properties)


@dataclass(frozen=True)
class GraphPattern:
    """Portable representation of a graph path pattern."""

    pattern_id: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.pattern_id, "pattern_id")
        if not self.node_ids:
            raise ValueError("node_ids must not be empty")
        if len(self.edge_ids) != len(self.node_ids) - 1:
            raise ValueError("edge_ids length must be exactly node_ids length minus one")


# ---------------------------------------------------------------------------
# Bounded traversal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphTraversal:
    """A bounded traversal specification."""

    start_node_id: str
    max_depth: int = 3
    relation_types: tuple[str, ...] = ()
    direction: str = "any"  # "any", "outgoing", "incoming"

    def __post_init__(self) -> None:
        _identifier(self.start_node_id, "start_node_id")
        if self.max_depth < 1 or self.max_depth > 1000:
            raise ValueError("max_depth must be between 1 and 1000")
        if self.direction not in ("any", "outgoing", "incoming"):
            raise ValueError("direction must be 'any', 'outgoing', or 'incoming'")
        for rt in self.relation_types:
            _label(rt)


# ---------------------------------------------------------------------------
# Path
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphPath:
    """A discovered path through the graph."""

    path_id: str
    node_ids: tuple[str, ...]
    edge_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.path_id, "path_id")
        if not self.node_ids:
            raise ValueError("GraphPath requires at least one node")
        if len(self.edge_ids) != len(self.node_ids) - 1:
            raise ValueError("edge_ids length must be node_ids length - 1")
        for nid in self.node_ids:
            _identifier(nid, "node_id")
        for eid in self.edge_ids:
            _identifier(eid, "edge_id")


# ---------------------------------------------------------------------------
# Transaction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphTransaction:
    """A transaction boundary for atomic graph operations."""

    transaction_id: str
    driver_name: str

    def __post_init__(self) -> None:
        if not self.transaction_id:
            raise ValueError("transaction_id must not be empty")
        if not self.driver_name:
            raise ValueError("driver_name must not be empty")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphSchema:
    """A schema snapshot for a graph domain."""

    schema_id: str
    labels: frozenset[str]
    relation_types: frozenset[str]

    def __post_init__(self) -> None:
        if not self.schema_id:
            raise ValueError("schema_id must not be empty")
        if not self.labels:
            raise ValueError("labels must not be empty")
        for label in self.labels:
            _label(label)
        for rt in self.relation_types:
            _label(rt)


# ---------------------------------------------------------------------------
# Query and result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphQuery:
    """A portable, parameterized graph query."""

    text: str
    parameters: dict[str, Any] = field(default_factory=dict)
    limit: int = 100
    deadline_ms: int = 5_000

    def __post_init__(self) -> None:
        _identifier(self.text, "text", max_len=10_000)
        _bounded(self.limit, "limit", 1, 10_000)
        _bounded(self.deadline_ms, "deadline_ms", 1, 120_000)
        _validate_parameterized_query(self.text, self.parameters)


@dataclass(frozen=True)
class GraphResult:
    """Portable graph result row."""

    node_id: str
    labels: tuple[str, ...]
    score: float
    rank: int

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id")
        if not self.labels:
            raise ValueError("labels must not be empty")
        _bounded(self.score, "score", 0.0, 1.0)
        _bounded(self.rank, "rank", 1, 10_000)
