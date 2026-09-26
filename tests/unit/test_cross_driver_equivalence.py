"""Shared graph adapter boundaries; these tests are not cross-service conformance.

The earlier suite compared private in-memory dictionaries labelled as external
services. Live graph traversal equivalence still requires actual service fixtures.
"""

from collections.abc import Callable

import pytest
from tests.unit.test_neo4j_community import Client as Neo4jClient
from tests.unit.test_orientdb_driver import Client as OrientDBClient

from af.framework.errors import PlatformError
from af.framework.managers import CapabilitySet
from af.graph.contracts import GraphFeature, GraphNode, GraphQuery, GraphTraversal
from af.graph.drivers.neo4j import Neo4jCommunityDriver
from af.graph.drivers.orientdb import OrientDBDriver


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Neo4jCommunityDriver("bolt://localhost:7687"),
        lambda: OrientDBDriver("remote://localhost:2424", "graphdb"),
    ],
)
def test_unconfigured_adapters_never_simulate_external_graphs(
    factory: Callable[[], object],
) -> None:
    driver = factory()
    assert not driver.verify_capabilities(set(GraphFeature)).values  # type: ignore[attr-defined]
    assert not hasattr(driver, "_nodes")
    assert not hasattr(driver, "_edges")
    with pytest.raises(PlatformError):
        driver.create_node("a", ("Person",), {})  # type: ignore[attr-defined]


def test_adapters_map_equivalent_native_projection_rows() -> None:
    neo, orient = Neo4jClient(), OrientDBClient()
    neo.rows = orient.rows = [{"node_id": "a", "labels": ["Person"], "score": 0.75}]
    first = Neo4jCommunityDriver("bolt://localhost", client=neo)
    second = OrientDBDriver("https://localhost:2480", "graphdb", client=orient)
    assert first.query(GraphQuery("RETURN 'a' AS node_id")) == second.query(
        GraphQuery("SELECT 'a' AS node_id")
    )


def test_only_implemented_capabilities_can_be_verified() -> None:
    def evidence() -> CapabilitySet:
        return CapabilitySet(feature.value for feature in GraphFeature)

    first = Neo4jCommunityDriver(
        "bolt://localhost", client=Neo4jClient(), capability_verifier=evidence
    )
    second = OrientDBDriver(
        "https://localhost", "graphdb", client=OrientDBClient(), capability_verifier=evidence
    )
    expected = CapabilitySet({GraphFeature.QUERY.value, GraphFeature.PERSISTENCE.value})
    assert first.verify_capabilities(set(GraphFeature)) == expected
    assert second.verify_capabilities(set(GraphFeature)) == expected
    first.close()
    assert not first.verify_capabilities(set(GraphFeature)).values


@pytest.mark.parametrize("label", ["Entity`", "A:B", "A); DROP CLASS", "a b", "a\nB"])
def test_schema_identifiers_cannot_be_interpolated_as_query_syntax(label: str) -> None:
    with pytest.raises(ValueError):
        GraphNode("a", (label,), {})


def test_traversal_contract_remains_bounded_without_claiming_an_execution_engine() -> None:
    traversal = GraphTraversal("a", max_depth=3, direction="outgoing", relation_types=("KNOWS",))
    assert traversal.max_depth == 3
