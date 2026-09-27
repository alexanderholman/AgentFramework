"""Neo4j protocol tests. These do not certify a live graph service."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from af.framework.errors import PlatformError
from af.graph.contracts import GraphFeature, GraphQuery
from af.graph.drivers.neo4j import Neo4jCommunityDriver


class Transaction:
    def __init__(self, client: "Client") -> None:
        self.client = client

    def run(self, text: str, parameters: Mapping[str, object]) -> Iterator[Mapping[str, object]]:
        self.client.calls.append((text, dict(parameters)))
        return iter(self.client.rows)

    def commit(self) -> None:
        self.client.commits += 1


class Session:
    def __init__(self, client: "Client") -> None:
        self.client = client

    @contextmanager
    def begin_transaction(self, *, timeout: float) -> Iterator[Transaction]:
        self.client.timeouts.append(timeout)
        yield Transaction(self.client)


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.rows: list[dict[str, object]] = []
        self.timeouts: list[float] = []
        self.commits = 0
        self.closed = False

    @contextmanager
    def session(self, **kwargs: object) -> Iterator[Session]:
        yield Session(self)

    def close(self) -> None:
        self.closed = True


def _driver(client: Client) -> Neo4jCommunityDriver:
    return Neo4jCommunityDriver(uri="bolt://localhost:7687", client=client)


def test_unconfigured_neo4j_never_claims_persistence_or_returns_fake_success() -> None:
    driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
    assert not driver.verify_capabilities(set(GraphFeature)).values
    with pytest.raises(PlatformError):
        driver.query(GraphQuery("RETURN 1"))
    with pytest.raises(PlatformError):
        driver.create_node("a", ("Person",), {})


def test_neo4j_binds_values_and_returns_bounded_actual_rows_with_deadline() -> None:
    client = Client()
    client.rows = [
        {"node_id": "a", "labels": ["Person"], "score": 0.7},
        {"node_id": "b", "labels": ["Person"], "score": 0.2},
    ]
    driver = _driver(client)
    value = "'; DROP DATABASE; --"
    result = driver.query(
        GraphQuery(
            "MATCH (n) WHERE n.name = $name RETURN n.af_id AS node_id, labels(n) AS labels",
            parameters={"name": value},
            limit=1,
            deadline_ms=250,
        )
    )
    assert [row.node_id for row in result] == ["a"]
    assert value not in client.calls[0][0]
    assert client.calls[0][1]["name"] == value
    assert client.timeouts == [0.25]
    assert client.commits == 1


def test_neo4j_crud_calls_database_and_rejects_schema_injection() -> None:
    client = Client()
    driver = _driver(client)
    client.rows = [{"node_id": "a"}]
    driver.create_node("a", ("Person",), {"name": "Alice"})
    assert "MERGE" in client.calls[-1][0]
    assert client.calls[-1][1]["properties"] == {"name": "Alice"}
    client.rows = [{"node_id": "a", "labels": ["Person"], "properties": {"name": "Alice"}}]
    node = driver.get_node("a")
    assert node is not None and node.properties == {"name": "Alice"}
    client.rows = [{"edge_id": "ab"}]
    driver.create_edge("ab", "a", "b", "KNOWS", {})
    assert client.calls[-1][1]["source_id"] == "a"
    client.rows = []
    driver.delete_edge("ab")
    driver.delete_node("a")
    with pytest.raises(ValueError):
        driver.create_node("a", ("Person`) DELETE n //",), {})
    driver.close()
    assert client.closed


def test_neo4j_requires_explicit_capability_evidence_and_sanitizes_failures() -> None:
    client = Client()
    driver = _driver(client)
    assert not driver.verify_capabilities(set(GraphFeature)).values

    def broken_session(**kwargs: Any) -> Any:
        raise RuntimeError("secret password")

    client.session = broken_session  # type: ignore[method-assign]
    with pytest.raises(PlatformError) as error:
        driver.delete_node("a")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("uri", ["", "http://localhost", "bolt://user:password@localhost"])
def test_neo4j_rejects_invalid_or_credential_bearing_uri(uri: str) -> None:
    with pytest.raises(ValueError):
        Neo4jCommunityDriver(uri=uri)
