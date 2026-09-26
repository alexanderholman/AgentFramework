"""OrientDB graph adapter using its parameterized JSON-over-HTTP command API."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import CapabilitySet
from af.graph.contracts import (
    _LITERALS_AND_COMMENTS,
    _PARAMETER_PLACEHOLDER_RE,
    GraphEdge,
    GraphFeature,
    GraphNode,
    GraphQuery,
    GraphResult,
    _identifier,
)


class OrientDBResponse(Protocol):
    def raise_for_status(self) -> None: ...
    def json(self) -> object: ...


class OrientDBHTTPClient(Protocol):
    def post(self, url: str, *, json: Mapping[str, object], timeout: float) -> OrientDBResponse: ...
    def close(self) -> None: ...


def _named_parameters(text: str) -> str:
    """Translate portable $name parameters without rewriting literals/comments."""
    pieces: list[str] = []
    position = 0
    for match in _LITERALS_AND_COMMENTS.finditer(text):
        pieces.append(
            _PARAMETER_PLACEHOLDER_RE.sub(
                lambda m: ":" + m.group()[1:], text[position : match.start()]
            )
        )
        pieces.append(match.group())
        position = match.end()
    pieces.append(_PARAMETER_PLACEHOLDER_RE.sub(lambda m: ":" + m.group()[1:], text[position:]))
    return "".join(pieces)


class OrientDBDriver:
    """Live REST CRUD/query adapter with explicit provisioning and evidence.

    Provision AFNode EXTENDS V and AFEdge EXTENDS E. Portable multi-label and
    property maps use af_labels/af_properties; edge type uses af_relation. Native
    queries return node_id, labels and optional score. No indexing API is claimed.
    """

    def __init__(
        self,
        uri: str,
        database: str,
        *,
        client: OrientDBHTTPClient | None = None,
        capability_verifier: Callable[[], CapabilitySet] | None = None,
    ) -> None:
        parsed = urlsplit(uri)
        if (
            parsed.scheme not in {"remote", "http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("OrientDB uri must use a credential-free remote, http or https scheme")
        _identifier(database, "database")
        if client is not None and parsed.scheme == "remote":
            raise ValueError("OrientDB live HTTP transport requires an http or https uri")
        self.uri, self.database = uri.rstrip("/"), database
        self._client = client
        self._verifier = capability_verifier
        self._closed = False

    def verify_capabilities(self, required: set[GraphFeature]) -> CapabilitySet:
        if self._closed or self._client is None or self._verifier is None:
            return CapabilitySet(())
        evidence = self._verifier()
        if not isinstance(evidence, CapabilitySet):
            raise TypeError("OrientDB verifier must return a CapabilitySet")
        implemented = {GraphFeature.QUERY.value, GraphFeature.PERSISTENCE.value}
        return CapabilitySet(
            evidence.values & implemented & {feature.value for feature in required}
        )

    def _run(
        self,
        command: str,
        parameters: Mapping[str, object],
        *,
        limit: int = 100,
        deadline_ms: int = 5000,
        write: bool = False,
    ) -> list[dict[str, Any]]:
        if self._closed or self._client is None:
            raise PlatformError(
                PlatformReason.CAPABILITY_UNAVAILABLE, stage="graph.orientdb.connection"
            )
        try:
            response = self._client.post(
                f"{self.uri}/command/{quote(self.database, safe='')}/sql",
                json={"command": command, "parameters": dict(parameters)},
                timeout=deadline_ms / 1000,
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("result") if isinstance(payload, Mapping) else payload
            if write and type(rows) is int:
                return []
            if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
                raise ValueError("OrientDB response is not a record collection")
            return [dict(row) for row in rows[:limit]]
        except Exception as error:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="graph.orientdb.operation",
                protected_cause=error,
            ) from None

    def query(self, query: GraphQuery, **kwargs: Any) -> list[GraphResult]:
        if kwargs:
            raise ValueError("OrientDB query options must use GraphQuery")
        command = (
            f"SELECT FROM ({_named_parameters(query.text)}\n) LIMIT {query.limit} "
            f"TIMEOUT {query.deadline_ms} EXCEPTION"
        )
        rows = self._run(
            command, query.parameters, limit=query.limit, deadline_ms=query.deadline_ms
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
                PlatformReason.INTEGRITY_FAILURE,
                stage="graph.orientdb.result",
                protected_cause=error,
            ) from None

    def create_node(
        self, node_id: str, labels: tuple[str, ...], properties: Mapping[str, Any]
    ) -> None:
        node = GraphNode(node_id, labels, dict(properties))
        self._run(
            "CREATE VERTEX AFNode SET af_id = :node_id, af_labels = :labels, "
            "af_properties = :properties",
            {"node_id": node.node_id, "labels": list(node.labels), "properties": node.properties},
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
        edge = GraphEdge(edge_id, source_id, target_id, relation_type, dict(properties))
        self._run(
            "CREATE EDGE AFEdge FROM (SELECT FROM AFNode WHERE af_id = :source_id) "
            "TO (SELECT FROM AFNode WHERE af_id = :target_id) SET af_id = :edge_id, "
            "af_relation = :relation_type, af_properties = :properties",
            {
                "edge_id": edge.edge_id,
                "source_id": edge.source_id,
                "target_id": edge.target_id,
                "relation_type": edge.relation_type,
                "properties": edge.properties,
            },
            write=True,
        )

    def get_node(self, node_id: str) -> GraphNode | None:
        _identifier(node_id, "node_id")
        rows = self._run(
            "SELECT af_id AS node_id, af_labels AS labels, af_properties AS properties "
            "FROM AFNode WHERE af_id = :node_id LIMIT 1 TIMEOUT 5000 EXCEPTION",
            {"node_id": node_id},
            limit=1,
        )
        if not rows:
            return None
        row = rows[0]
        return GraphNode(row["node_id"], tuple(row["labels"]), dict(row["properties"]))

    def get_edge(self, edge_id: str) -> GraphEdge | None:
        _identifier(edge_id, "edge_id")
        rows = self._run(
            "SELECT af_id AS edge_id, out.af_id AS source_id, in.af_id AS target_id, "
            "af_relation AS relation_type, af_properties AS properties FROM AFEdge "
            "WHERE af_id = :edge_id LIMIT 1 TIMEOUT 5000 EXCEPTION",
            {"edge_id": edge_id},
            limit=1,
        )
        if not rows:
            return None
        row = rows[0]
        return GraphEdge(
            row["edge_id"],
            row["source_id"],
            row["target_id"],
            row["relation_type"],
            dict(row["properties"]),
        )

    def delete_node(self, node_id: str) -> None:
        _identifier(node_id, "node_id")
        self._run("DELETE VERTEX AFNode WHERE af_id = :node_id", {"node_id": node_id}, write=True)

    def delete_edge(self, edge_id: str) -> None:
        _identifier(edge_id, "edge_id")
        self._run("DELETE EDGE AFEdge WHERE af_id = :edge_id", {"edge_id": edge_id}, write=True)

    def close(self) -> None:
        if not self._closed and self._client is not None:
            self._client.close()
        self._closed = True
