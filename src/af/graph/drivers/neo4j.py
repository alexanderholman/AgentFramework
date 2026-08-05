from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from af.framework.managers import CapabilitySet
from af.graph.contracts import (
    GraphEdge,
    GraphFeature,
    GraphNode,
    GraphQuery,
    GraphResult,
)
from af.graph.manager import GraphDriver


# Neo4j Community capabilities
_NEO4J_COMMUNITY_CAPABILITIES: frozenset[str] = frozenset({
    GraphFeature.QUERY.value,
    GraphFeature.PERSISTENCE.value,
})


@dataclass
class Neo4jCommunityDriver(GraphDriver):
    """In-memory stub for Neo4j Community Edition.

    Conforms to the GraphDriver protocol. When the neo4j package is
    installed, this driver can be wired to a live Bolt connection.
    For now it operates against an in-memory store to enable offline
    testing of the portable graph contracts.
    """

    uri: str
    community_edition: bool = True

    def __post_init__(self) -> None:
        if not self.uri:
            raise ValueError("uri must not be empty")
        if not self.uri.startswith("bolt://"):
            raise ValueError("uri must use bolt:// scheme for Neo4j")

    # -- GraphDriver protocol ------------------------------------------------

    def verify_capabilities(self, required: set[GraphFeature]) -> CapabilitySet:
        available = {f.value for f in required} & _NEO4J_COMMUNITY_CAPABILITIES
        return CapabilitySet(values=frozenset(available))

    def query(self, query: GraphQuery) -> Sequence[GraphResult]:
        # Reject queries with untrusted text patterns
        if self._contains_untrusted(query.text):
            raise ValueError(f"untrusted query text rejected: {query.text!r}")
        # Stub: return empty results (no live database)
        return []

    def create_node(self, node_id: str, labels: tuple[str, ...], properties: Mapping[str, Any]) -> None:
        pass

    def create_edge(
        self,
        edge_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
        properties: Mapping[str, Any],
    ) -> None:
        pass

    def delete_node(self, node_id: str) -> None:
        pass

    def delete_edge(self, edge_id: str) -> None:
        pass

    # -- In-memory store for testing ----------------------------------------

    def __init__(self, uri: str, community_edition: bool = True) -> None:
        self.uri = uri
        self.community_edition = community_edition
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[str, GraphEdge] = {}

        if not uri:
            raise ValueError("uri must not be empty")
        if not uri.startswith("bolt://"):
            raise ValueError("uri must use bolt:// scheme for Neo4j")

    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        return self._edges.get(edge_id)

    def create_node(self, node_id: str, labels: tuple[str, ...], properties: Mapping[str, Any]) -> None:
        self._nodes[node_id] = GraphNode(
            node_id=node_id,
            labels=labels,
            properties=dict(properties),
        )

    def create_edge(
        self,
        edge_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
        properties: Mapping[str, Any],
    ) -> None:
        self._edges[edge_id] = GraphEdge(
            edge_id=edge_id,
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            properties=dict(properties),
        )

    def delete_node(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)

    def delete_edge(self, edge_id: str) -> None:
        self._edges.pop(edge_id, None)

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _contains_untrusted(text: str) -> bool:
        patterns = (";", "--", "/*", "*/", "DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "EXEC")
        upper = text.upper()
        return any(p.upper() in upper for p in patterns)
