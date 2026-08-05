from __future__ import annotations

import pytest

from af.framework.managers import CapabilitySet
from af.graph.contracts import GraphFeature, GraphQuery
from af.graph.drivers.neo4j import Neo4jCommunityDriver


class TestNeo4jCommunityDriver:
    """Tests for Neo4j Community driver conforming to GraphDriver protocol."""

    def test_driver_reports_query_capability(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        result = driver.verify_capabilities({GraphFeature.QUERY})
        assert isinstance(result, CapabilitySet)
        assert GraphFeature.QUERY.value in result.values

    def test_driver_reports_persistence_capability(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        result = driver.verify_capabilities({GraphFeature.PERSISTENCE})
        assert isinstance(result, CapabilitySet)
        assert GraphFeature.PERSISTENCE.value in result.values

    def test_driver_reports_missing_indexing_capability(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        result = driver.verify_capabilities({GraphFeature.INDEXING})
        assert isinstance(result, CapabilitySet)
        assert GraphFeature.INDEXING.value not in result.values

    def test_driver_reports_missing_cancellation_capability(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        assert driver.community_edition is True
        result = driver.verify_capabilities({GraphFeature.CANCELLATION})
        assert isinstance(result, CapabilitySet)
        assert GraphFeature.CANCELLATION.value not in result.values

    def test_driver_query_returns_empty_results(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        query = GraphQuery(text="MATCH (n) RETURN n", limit=10)
        results = driver.query(query)
        assert isinstance(results, list)
        assert len(results) == 0

    def test_driver_query_rejects_untrusted_text(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        with pytest.raises(ValueError, match="untrusted"):
            driver.query(
                GraphQuery(
                    text="MATCH (n) RETURN n; DROP DATABASE",
                    limit=10,
                )
            )

    def test_driver_create_node_stores_node(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        driver.create_node(
            node_id="entity-001",
            labels=("Entity",),
            properties={"name": "test"},
        )
        assert driver.get_node("entity-001") is not None

    def test_driver_create_edge_stores_edge(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        driver.create_node(
            node_id="entity-001",
            labels=("Entity",),
            properties={},
        )
        driver.create_node(
            node_id="entity-002",
            labels=("Entity",),
            properties={},
        )
        driver.create_edge(
            edge_id="rel-001",
            source_id="entity-001",
            target_id="entity-002",
            relation_type="KNOWS",
            properties={},
        )
        edge = driver.get_edge("rel-001")
        assert edge is not None
        assert edge.edge_id == "rel-001"

    def test_driver_delete_node_removes_node(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        driver.create_node(
            node_id="entity-001",
            labels=("Entity",),
            properties={},
        )
        driver.delete_node("entity-001")
        assert driver.get_node("entity-001") is None

    def test_driver_delete_edge_removes_edge(self) -> None:
        driver = Neo4jCommunityDriver(uri="bolt://localhost:7687")
        driver.create_node(
            node_id="entity-001",
            labels=("Entity",),
            properties={},
        )
        driver.create_node(
            node_id="entity-002",
            labels=("Entity",),
            properties={},
        )
        driver.create_edge(
            edge_id="rel-001",
            source_id="entity-001",
            target_id="entity-002",
            relation_type="KNOWS",
            properties={},
        )
        driver.delete_edge("rel-001")
        assert driver.get_edge("rel-001") is None

    def test_driver_requires_uri(self) -> None:
        with pytest.raises(ValueError, match="uri"):
            Neo4jCommunityDriver(uri="")

    def test_driver_uri_must_be_bolt(self) -> None:
        with pytest.raises(ValueError, match="bolt"):
            Neo4jCommunityDriver(uri="http://localhost:7474")
