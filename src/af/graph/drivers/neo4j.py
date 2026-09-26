"""Neo4j Community adapter using an explicitly injected official-driver client."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from itertools import islice
from typing import Any, Protocol
from urllib.parse import urlsplit

from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import CapabilitySet
from af.graph.contracts import (
    GraphEdge,
    GraphFeature,
    GraphNode,
    GraphPath,
    GraphQuery,
    GraphResult,
    GraphTraversal,
    _identifier,
)
from af.graph.traversal import edges_from_rows, execute_traversal


class Neo4jTransaction(Protocol):
    def run(self, query: str, parameters: Mapping[str, object]) -> Iterable[Mapping[str, Any]]: ...
    def commit(self) -> None: ...


class Neo4jSession(Protocol):
    def begin_transaction(self, *, timeout: float) -> AbstractContextManager[Neo4jTransaction]: ...


class Neo4jClient(Protocol):
    def session(self, **kwargs: object) -> AbstractContextManager[Neo4jSession]: ...
    def close(self) -> None: ...


class Neo4jCommunityDriver:
    """Real Cypher CRUD/query transport; no process-local persistence fallback.

    Native query rows must project ``node_id``, ``labels`` and optional ``score``.
    Authentication, schema constraints, transport timeouts and capability evidence
    are supplied by the deployment. A URI alone enables no capabilities.
    """

    def __init__(
        self,
        uri: str,
        community_edition: bool = True,
        *,
        client: Neo4jClient | None = None,
        database: str | None = None,
        capability_verifier: Callable[[], CapabilitySet] | None = None,
    ) -> None:
        parsed = urlsplit(uri)
        if (
            parsed.scheme not in {"bolt", "bolt+s", "neo4j", "neo4j+s"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Neo4j uri must use a credential-free bolt or neo4j scheme")
        self.uri = uri
        self.community_edition = community_edition
        self._client = client
        self._database = database
        self._verifier = capability_verifier
        self._closed = False

    def verify_capabilities(self, required: set[GraphFeature]) -> CapabilitySet:
        if self._closed or self._client is None or self._verifier is None:
            return CapabilitySet(())
        evidence = self._verifier()
        if not isinstance(evidence, CapabilitySet):
            raise TypeError("Neo4j verifier must return a CapabilitySet")
        implemented = {
            GraphFeature.QUERY.value,
            GraphFeature.PERSISTENCE.value,
            GraphFeature.TRAVERSAL.value,
        }
        return CapabilitySet(
            evidence.values & implemented & {feature.value for feature in required}
        )

    def _run(
        self,
        text: str,
        parameters: Mapping[str, object],
        *,
        limit: int = 100,
        deadline_ms: int = 5000,
        write: bool = False,
    ) -> list[dict[str, Any]]:
        if self._closed or self._client is None:
            raise PlatformError(
                PlatformReason.CAPABILITY_UNAVAILABLE, stage="graph.neo4j.connection"
            )
        options: dict[str, object] = {"default_access_mode": "WRITE" if write else "READ"}
        if self._database is not None:
            options["database"] = self._database
        try:
            with (
                self._client.session(**options) as session,
                session.begin_transaction(timeout=deadline_ms / 1000) as transaction,
            ):
                rows = [dict(row) for row in islice(transaction.run(text, parameters), limit)]
                transaction.commit()
                return rows
        except Exception as error:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="graph.neo4j.operation",
                protected_cause=error,
            ) from None

    def query(self, query: GraphQuery, **kwargs: Any) -> list[GraphResult]:
        if kwargs:
            raise ValueError("Neo4j query options must use GraphQuery")
        if "af_result_limit" in query.parameters:
            raise ValueError("Neo4j query uses a reserved parameter")
        rows = self._run(
            f"CALL {{ {query.text}\n }} RETURN * LIMIT $af_result_limit",
            {**query.parameters, "af_result_limit": query.limit},
            limit=query.limit,
            deadline_ms=query.deadline_ms,
        )
        try:
            return [
                GraphResult(
                    node_id=row["node_id"],
                    labels=tuple(row["labels"]),
                    score=row.get("score", 1.0),
                    rank=rank,
                )
                for rank, row in enumerate(rows, 1)
            ]
        except (KeyError, TypeError, ValueError) as error:
            raise PlatformError(
                PlatformReason.INTEGRITY_FAILURE, stage="graph.neo4j.result", protected_cause=error
            ) from None

    @staticmethod
    def _properties(properties: Mapping[str, Any]) -> dict[str, Any]:
        if "af_id" in properties:
            raise ValueError("graph property name af_id is reserved")
        return dict(properties)

    def traverse(self, traversal: GraphTraversal) -> list[GraphPath]:
        if not isinstance(traversal, GraphTraversal):
            raise TypeError("Neo4j traversal requires GraphTraversal")

        def neighbors(node_id: str, limit: int, deadline_ms: int) -> list[GraphEdge]:
            predicate = {
                "outgoing": "a.af_id = $node_id",
                "incoming": "b.af_id = $node_id",
                "any": "(a.af_id = $node_id OR b.af_id = $node_id)",
            }[traversal.direction]
            parameters: dict[str, object] = {"node_id": node_id, "limit": limit}
            if traversal.relation_types:
                predicate += " AND type(e) IN $relation_types"
                parameters["relation_types"] = list(traversal.relation_types)
            rows = self._run(
                "MATCH (a:AFNode)-[e]->(b:AFNode) WHERE "
                + predicate
                + " RETURN e.af_id AS edge_id, a.af_id AS source_id, b.af_id AS target_id, "
                "type(e) AS relation_type ORDER BY edge_id, source_id, target_id LIMIT $limit",
                parameters,
                limit=limit,
                deadline_ms=deadline_ms,
            )
            return edges_from_rows(rows)

        return execute_traversal(traversal, neighbors)

    def create_node(
        self, node_id: str, labels: tuple[str, ...], properties: Mapping[str, Any]
    ) -> None:
        node = GraphNode(node_id, labels, self._properties(properties))
        label_text = ":".join(f"`{label}`" for label in node.labels)
        self._run(
            f"MERGE (n:AFNode {{af_id: $node_id}}) SET n += $properties "
            f"SET n:{label_text} RETURN n.af_id AS node_id",
            {"node_id": node.node_id, "properties": node.properties},
            limit=1,
            write=True,
        )

    def create_edge(
        self,
        edge_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
        properties: Mapping[str, Any],
    ) -> None:
        edge = GraphEdge(edge_id, source_id, target_id, relation_type, self._properties(properties))
        rows = self._run(
            "MATCH (a:AFNode {af_id: $source_id}), (b:AFNode {af_id: $target_id}) "
            f"MERGE (a)-[e:`{edge.relation_type}` {{af_id: $edge_id}}]->(b) "
            "SET e += $properties RETURN e.af_id AS edge_id",
            {
                "edge_id": edge.edge_id,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "properties": edge.properties,
            },
            limit=1,
            write=True,
        )
        if not rows:
            raise ValueError("graph edge endpoints must exist")

    def get_node(self, node_id: str) -> GraphNode | None:
        _identifier(node_id, "node_id")
        rows = self._run(
            "MATCH (n:AFNode {af_id: $node_id}) RETURN n.af_id AS node_id, "
            "[label IN labels(n) WHERE label <> 'AFNode'] AS labels, properties(n) AS properties "
            "LIMIT 1",
            {"node_id": node_id},
            limit=1,
        )
        if not rows:
            return None
        row = rows[0]
        properties = dict(row["properties"])
        properties.pop("af_id", None)
        return GraphNode(row["node_id"], tuple(row["labels"]), properties)

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        _identifier(edge_id, "edge_id")
        rows = self._run(
            "MATCH (a:AFNode)-[e {af_id: $edge_id}]->(b:AFNode) "
            "RETURN e.af_id AS edge_id, a.af_id AS source_id, b.af_id AS target_id, "
            "type(e) AS relation_type, properties(e) AS properties LIMIT 1",
            {"edge_id": edge_id},
            limit=1,
        )
        if not rows:
            return None
        row = rows[0]
        properties = dict(row["properties"])
        properties.pop("af_id", None)
        return GraphEdge(
            row["edge_id"], row["source_id"], row["target_id"], row["relation_type"], properties
        )

    def delete_node(self, node_id: str) -> None:
        _identifier(node_id, "node_id")
        self._run(
            "MATCH (n:AFNode {af_id: $node_id}) DETACH DELETE n", {"node_id": node_id}, write=True
        )

    def delete_edge(self, edge_id: str) -> None:
        _identifier(edge_id, "edge_id")
        self._run(
            "MATCH (:AFNode)-[e {af_id: $edge_id}]->(:AFNode) DELETE e",
            {"edge_id": edge_id},
            write=True,
        )

    def close(self) -> None:
        if not self._closed and self._client is not None:
            self._client.close()
        self._closed = True
