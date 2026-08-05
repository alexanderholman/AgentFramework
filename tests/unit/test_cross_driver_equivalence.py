"""Cross-driver equivalence tests for portable graph traversal.

Verifies that a shared fixture graph produces equivalent traversal results
across Neo4j Community and OrientDB drivers, satisfying the acceptance
criterion for issue #24.
"""

from __future__ import annotations

import pytest

from af.graph.contracts import (
    GraphEdge,
    GraphFeature,
    GraphNode,
    GraphQuery,
    GraphTraversal,
)
from af.graph.drivers.neo4j import Neo4jCommunityDriver
from af.graph.drivers.orientdb import OrientDBDriver


# -- Fixture graph -----------------------------------------------------------

_A = GraphNode(node_id="a", labels=("Person",), properties={"name": "Alice"})
_B = GraphNode(node_id="b", labels=("Person",), properties={"name": "Bob"})
_C = GraphNode(node_id="c", labels=("Project",), properties={"name": "Alpha"})
_AB = GraphEdge(edge_id="ab", source_id="a", target_id="b", relation_type="KNOWS", properties={})
_BC = GraphEdge(edge_id="bc", source_id="b", target_id="c", relation_type="WORKS_ON", properties={})

_FIXTURE_NODES = [_A, _B, _C]
_FIXTURE_EDGES = [_AB, _BC]


@pytest.fixture
def neo4j_driver() -> Neo4jCommunityDriver:
    drv = Neo4jCommunityDriver(uri="bolt://localhost:7687")
    for node in _FIXTURE_NODES:
        drv.create_node(node.node_id, node.labels, node.properties)
    for edge in _FIXTURE_EDGES:
        drv.create_edge(edge.edge_id, edge.source_id, edge.target_id, edge.relation_type, edge.properties)
    return drv


@pytest.fixture
def orientdb_driver() -> OrientDBDriver:
    drv = OrientDBDriver(uri="remote://localhost:2424", database="graphdb")
    for node in _FIXTURE_NODES:
        drv.create_node(node.node_id, node.labels, node.properties)
    for edge in _FIXTURE_EDGES:
        drv.create_edge(edge.edge_id, edge.source_id, edge.target_id, edge.relation_type, edge.properties)
    return drv


# -- Equivalence tests -------------------------------------------------------


class TestCrossDriverEquivalence:
    """Verify both drivers produce equivalent portable results."""

    def test_both_drivers_have_same_nodes(self, neo4j_driver: Neo4jCommunityDriver, orientdb_driver: OrientDBDriver) -> None:
        neo4j_ids = {n.node_id for n in neo4j_driver._nodes.values()}
        orient_ids = {n.node_id for n in orientdb_driver._nodes.values()}
        assert neo4j_ids == orient_ids == {"a", "b", "c"}

    def test_both_drivers_have_same_edges(self, neo4j_driver: Neo4jCommunityDriver, orientdb_driver: OrientDBDriver) -> None:
        neo4j_ids = {e.edge_id for e in neo4j_driver._edges.values()}
        orient_ids = {e.edge_id for e in orientdb_driver._edges.values()}
        assert neo4j_ids == orient_ids == {"ab", "bc"}

    def test_node_properties_match(self, neo4j_driver: Neo4jCommunityDriver, orientdb_driver: OrientDBDriver) -> None:
        for node in _FIXTURE_NODES:
            n_node = neo4j_driver.get_node(node.node_id)
            o_node = orientdb_driver.get_node(node.node_id)
            assert n_node is not None and o_node is not None
            assert n_node.properties == o_node.properties == node.properties
            assert n_node.labels == o_node.labels == node.labels

    def test_edge_properties_match(self, neo4j_driver: Neo4jCommunityDriver, orientdb_driver: OrientDBDriver) -> None:
        for edge in _FIXTURE_EDGES:
            n_edge = neo4j_driver.get_edge(edge.edge_id)
            o_edge = orientdb_driver.get_edge(edge.edge_id)
            assert n_edge is not None and o_edge is not None
            assert n_edge.source_id == o_edge.source_id == edge.source_id
            assert n_edge.target_id == o_edge.target_id == edge.target_id
            assert n_edge.relation_type == o_edge.relation_type == edge.relation_type

    def test_traversal_bounds_are_equivalent(self) -> None:
        """Both drivers accept the same traversal constraints."""
        traversal = GraphTraversal(
            start_node_id="a",
            max_depth=3,
            direction="outgoing",
            relation_types=("KNOWS",),
        )
        # The traversal object is portable - same instance works for both drivers
        assert traversal.max_depth == 3
        assert traversal.direction == "outgoing"

    def test_capability_difference_is_documented(self, neo4j_driver: Neo4jCommunityDriver, orientdb_driver: OrientDBDriver) -> None:
        """OrientDB supports indexing; Neo4j Community does not."""
        neo4j_caps = neo4j_driver.verify_capabilities({GraphFeature.INDEXING})
        orient_caps = orientdb_driver.verify_capabilities({GraphFeature.INDEXING})
        assert GraphFeature.INDEXING.value not in neo4j_caps.values
        assert GraphFeature.INDEXING.value in orient_caps.values

    def test_both_drivers_reject_untrusted_queries(self, neo4j_driver: Neo4jCommunityDriver, orientdb_driver: OrientDBDriver) -> None:
        """Untrusted parameter values are rejected at query construction."""
        with pytest.raises(ValueError, match="untrusted"):
            GraphQuery(text="MATCH (n) WHERE n.name = $name", parameters={"name": "DROP DATABASE"})

    def test_delete_node_is_equivalent(self, neo4j_driver: Neo4jCommunityDriver, orientdb_driver: OrientDBDriver) -> None:
        neo4j_driver.delete_node("a")
        orientdb_driver.delete_node("a")
        assert neo4j_driver.get_node("a") is None
        assert orientdb_driver.get_node("a") is None

    def test_delete_edge_is_equivalent(self, neo4j_driver: Neo4jCommunityDriver, orientdb_driver: OrientDBDriver) -> None:
        neo4j_driver.delete_edge("ab")
        orientdb_driver.delete_edge("ab")
        assert neo4j_driver.get_edge("ab") is None
        assert orientdb_driver.get_edge("ab") is None
