from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence, Tuple


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
# Feature enum
# ---------------------------------------------------------------------------

class GraphFeature(str, Enum):
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
    labels: Tuple[str, ...]
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
    node_ids: Tuple[str, ...]
    edge_ids: Tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.pattern_id, "pattern_id")
        if not self.node_ids:
            raise ValueError("node_ids must not be empty")
        if len(self.edge_ids) != len(self.node_ids) - 1:
            raise ValueError("edge_ids length must be exactly node_ids length minus one")


@dataclass(frozen=True)
class GraphQuery:
    """Portable graph query envelope."""

    text: str
    limit: int = 100
    deadline_ms: int = 5_000

    def __post_init__(self) -> None:
        _identifier(self.text, "text", max_len=10_000)
        _bounded(self.limit, "limit", 1, 10_000)
        _bounded(self.deadline_ms, "deadline_ms", 1, 120_000)


@dataclass(frozen=True)
class GraphResult:
    """Portable graph result row."""

    node_id: str
    labels: Tuple[str, ...]
    score: float
    rank: int

    def __post_init__(self) -> None:
        _identifier(self.node_id, "node_id")
        if not self.labels:
            raise ValueError("labels must not be empty")
        _bounded(self.score, "score", 0.0, 1.0)
        _bounded(self.rank, "rank", 1, 10_000)
