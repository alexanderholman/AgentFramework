"""Bounded breadth-first discovery over real backend adjacency reads."""

from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from collections.abc import Callable
from time import monotonic
from typing import Any

from af.framework.errors import PlatformError, PlatformReason
from af.graph.contracts import GraphEdge, GraphPath, GraphTraversal


def edges_from_rows(rows: list[dict[str, Any]]) -> list[GraphEdge]:
    try:
        return [
            GraphEdge(row["edge_id"], row["source_id"], row["target_id"], row["relation_type"], {})
            for row in rows
        ]
    except (KeyError, TypeError, ValueError) as error:
        raise PlatformError(
            PlatformReason.INTEGRITY_FAILURE, stage="graph.traversal.result", protected_cause=error
        ) from None


def execute_traversal(
    specification: GraphTraversal,
    neighbors: Callable[[str, int, int], list[GraphEdge]],
    *,
    clock: Callable[[], float] = monotonic,
) -> list[GraphPath]:
    """Return one shortest discovery path per node, excluding the starting node.

    Adjacency calls receive (node_id, edge limit, remaining deadline milliseconds).
    Limits/deadlines are admission and result bounds, never thread preemption. Each
    hop is a separate backend read, so concurrent graph edits are not a snapshot.
    """
    if not isinstance(specification, GraphTraversal):
        raise TypeError("graph traversal requires GraphTraversal")
    deadline = clock() + specification.deadline_ms / 1000
    pending: deque[tuple[tuple[str, ...], tuple[str, ...]]] = deque(
        [((specification.start_node_id,), ())]
    )
    seen = {specification.start_node_id}
    results: list[GraphPath] = []
    scanned = 0
    while pending:
        nodes, edge_ids = pending.popleft()
        if len(edge_ids) >= specification.max_depth:
            continue
        remaining = deadline - clock()
        if remaining <= 0:
            raise PlatformError(PlatformReason.CANCELLED, stage="graph.traversal.deadline")
        budget = specification.max_edges - scanned
        if budget <= 0:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE, stage="graph.traversal.edge-budget"
            )
        rows = neighbors(nodes[-1], budget + 1, max(1, math.ceil(remaining * 1000)))
        if clock() >= deadline:
            raise PlatformError(PlatformReason.CANCELLED, stage="graph.traversal.deadline")
        if len(rows) > budget:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE, stage="graph.traversal.edge-budget"
            )
        scanned += len(rows)
        for edge in sorted(rows, key=lambda item: (item.edge_id, item.source_id, item.target_id)):
            outgoing = edge.source_id == nodes[-1]
            incoming = edge.target_id == nodes[-1]
            if (
                not (outgoing or incoming)
                or (specification.direction == "outgoing" and not outgoing)
                or (specification.direction == "incoming" and not incoming)
                or (
                    specification.relation_types
                    and edge.relation_type not in specification.relation_types
                )
            ):
                raise PlatformError(
                    PlatformReason.INTEGRITY_FAILURE, stage="graph.traversal.result"
                )
            destination = edge.target_id if outgoing else edge.source_id
            if destination in seen:
                continue
            seen.add(destination)
            next_nodes, next_edges = (*nodes, destination), (*edge_ids, edge.edge_id)
            identity = hashlib.sha256(
                json.dumps([next_nodes, next_edges], separators=(",", ":")).encode()
            ).hexdigest()
            results.append(GraphPath(identity, next_nodes, next_edges))
            if len(results) >= specification.max_results:
                return results
            pending.append((next_nodes, next_edges))
    return results
