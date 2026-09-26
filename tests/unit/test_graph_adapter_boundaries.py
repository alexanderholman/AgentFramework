"""External graph adapter protocol results, error boundaries and lifecycle."""

from typing import Any

import pytest
from tests.unit.test_neo4j_community import Client as NeoClient
from tests.unit.test_orientdb_driver import Client as OrientClient
from tests.unit.test_orientdb_driver import Response

from af.framework.errors import PlatformError
from af.graph.contracts import GraphFeature, GraphQuery
from af.graph.drivers.neo4j import Neo4jCommunityDriver
from af.graph.drivers.orientdb import OrientDBDriver


@pytest.mark.parametrize("kind", ["neo4j", "orientdb"])
def test_graph_get_edge_preserves_actual_backend_values_and_missing_rows(kind: str) -> None:
    client: Any = NeoClient() if kind == "neo4j" else OrientClient()
    driver = (
        Neo4jCommunityDriver("bolt://localhost", client=client, database="neo4j")
        if kind == "neo4j"
        else OrientDBDriver("https://localhost", "graph", client=client)
    )
    assert driver.get_edge("ab") is None
    assert driver.get_node("a") is None
    client.rows = [
        {
            "edge_id": "ab",
            "source_id": "a",
            "target_id": "b",
            "relation_type": "KNOWS",
            "properties": {"weight": 0.5},
        }
    ]
    edge = driver.get_edge("ab")
    assert edge is not None and edge.properties == {"weight": 0.5}
    assert edge.source_id == "a" and edge.target_id == "b"
    with pytest.raises(ValueError):
        driver.query(GraphQuery("RETURN 1"), unknown=True)
    client.rows = [{"unexpected": "row"}]
    with pytest.raises(PlatformError):
        driver.query(GraphQuery("RETURN 1"))
    driver.close()
    driver.close()
    with pytest.raises(PlatformError):
        driver.delete_node("a")


def test_neo4j_rejects_reserved_properties_parameters_and_missing_endpoints() -> None:
    driver = Neo4jCommunityDriver("bolt://localhost", client=NeoClient())
    with pytest.raises(ValueError):
        driver.create_node("a", ("Person",), {"af_id": "other"})
    with pytest.raises(ValueError):
        driver.create_edge("ab", "a", "b", "KNOWS", {})
    with pytest.raises(ValueError):
        driver.query(GraphQuery("RETURN $af_result_limit", parameters={"af_result_limit": 1}))


@pytest.mark.parametrize("kind", ["neo4j", "orientdb"])
def test_graph_adapter_rejects_invalid_capability_evidence(kind: str) -> None:
    def verifier() -> None:
        return None

    driver = (
        Neo4jCommunityDriver("bolt://localhost", client=NeoClient(), capability_verifier=verifier)
        if kind == "neo4j"
        else OrientDBDriver(
            "https://localhost", "graph", client=OrientClient(), capability_verifier=verifier
        )
    )  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        driver.verify_capabilities(set(GraphFeature))


def test_orientdb_checks_protocol_results_and_http_failures() -> None:
    class Controlled(OrientClient):
        result: object = None
        failure = False

        def post(self, url: str, **kwargs: Any) -> Response:
            if self.failure:
                raise RuntimeError("secret transport details")
            return Response(self.result)

    client = Controlled()
    driver = OrientDBDriver("https://localhost", "graph", client=client)
    client.result = 1
    driver.delete_node("a")
    client.result = "invalid"
    with pytest.raises(PlatformError):
        driver.query(GraphQuery("SELECT FROM AFNode"))
    client.failure = True
    with pytest.raises(PlatformError) as error:
        driver.delete_node("a")
    assert "secret" not in str(error.value)
    with pytest.raises(ValueError):
        OrientDBDriver("remote://localhost", "graph", client=client)
