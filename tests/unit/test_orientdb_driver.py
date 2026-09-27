"""OrientDB REST protocol tests; no live service certification is implied."""

from collections.abc import Mapping

import pytest

from af.framework.errors import PlatformError
from af.graph.contracts import GraphFeature, GraphQuery
from af.graph.drivers.orientdb import OrientDBDriver


class Response:
    def __init__(self, result: object) -> None:
        self.result = result

    def raise_for_status(self) -> None:
        pass

    def json(self) -> object:
        return {"result": self.result}


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object], float]] = []
        self.rows: list[dict[str, object]] = []
        self.closed = False

    def post(self, url: str, *, json: Mapping[str, object], timeout: float) -> Response:
        self.calls.append((url, json, timeout))
        return Response(self.rows)

    def close(self) -> None:
        self.closed = True


def test_unconfigured_orientdb_never_claims_persistence_or_success() -> None:
    driver = OrientDBDriver(uri="remote://localhost:2424", database="graphdb")
    assert not driver.verify_capabilities(set(GraphFeature)).values
    with pytest.raises(PlatformError):
        driver.query(GraphQuery("SELECT FROM V"))
    with pytest.raises(PlatformError):
        driver.create_node("a", ("Person",), {})


def test_orientdb_binds_json_parameters_deadline_and_bounded_real_results() -> None:
    client = Client()
    client.rows = [
        {"node_id": "a", "labels": ["Person"], "score": 0.8},
        {"node_id": "b", "labels": ["Person"], "score": 0.3},
    ]
    driver = OrientDBDriver(uri="https://orient.example:2480", database="graphdb", client=client)
    value = "'; DROP CLASS; --"
    results = driver.query(
        GraphQuery(
            "SELECT af_id AS node_id, af_labels AS labels FROM AFNode WHERE name = $name",
            parameters={"name": value},
            limit=1,
            deadline_ms=350,
        )
    )
    assert [row.node_id for row in results] == ["a"]
    url, payload, timeout = client.calls[0]
    assert url == "https://orient.example:2480/command/graphdb/sql"
    assert payload["parameters"] == {"name": value}
    assert value not in str(payload["command"])
    assert ":name" in str(payload["command"])
    assert "LIMIT 1 TIMEOUT 350 EXCEPTION" in str(payload["command"])
    assert timeout == 0.35


def test_orientdb_crud_reaches_backend_with_portable_label_storage() -> None:
    client = Client()
    driver = OrientDBDriver(uri="https://orient.example", database="graphdb", client=client)
    driver.create_node("a", ("Person", "Researcher"), {"name": "Alice"})
    assert "CREATE VERTEX AFNode" in str(client.calls[-1][1]["command"])
    client.rows = [
        {"node_id": "a", "labels": ["Person", "Researcher"], "properties": {"name": "Alice"}}
    ]
    node = driver.get_node("a")
    assert node is not None and node.labels == ("Person", "Researcher")
    driver.create_edge("ab", "a", "b", "KNOWS", {})
    assert "CREATE EDGE AFEdge" in str(client.calls[-1][1]["command"])
    driver.delete_edge("ab")
    driver.delete_node("a")
    driver.close()
    assert client.closed


@pytest.mark.parametrize("uri", ["", "ftp://localhost", "https://user:secret@host"])
def test_orientdb_rejects_invalid_or_credential_bearing_uri(uri: str) -> None:
    with pytest.raises(ValueError):
        OrientDBDriver(uri=uri, database="graphdb")
