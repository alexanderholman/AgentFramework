from __future__ import annotations

from typing import Any, Mapping, Sequence, Set

import pytest

from af.framework.managers import CapabilitySet, DriverRegistration
from af.graph.contracts import GraphFeature, GraphQuery, GraphResult
from af.graph.manager import GraphDriver, GraphManager


# ---------------------------------------------------------------------------
# Minimal test driver
# ---------------------------------------------------------------------------

class _StubGraphDriver:
    def __init__(self, capabilities: Set[GraphFeature]) -> None:
        self.capabilities = capabilities

    def verify_capabilities(self, required: Set[GraphFeature]) -> CapabilitySet[GraphFeature]:
        missing = required - self.capabilities
        return CapabilitySet(supported=self.capabilities & required, missing=missing)

    def query(self, query: GraphQuery, **kwargs: Any) -> list[GraphResult]:
        return []

    def create_node(self, node_id: str, labels: Sequence[str], properties: Mapping[str, Any]) -> None:
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_manager_rejects_empty_registry() -> None:
    with pytest.raises(ValueError):
        GraphManager({})


def test_manager_selects_default_driver() -> None:
    driver = _StubGraphDriver({GraphFeature.QUERY})
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(
                driver=driver,
                capabilities={GraphFeature.QUERY},
            ),
        },
        default_name="neo4j",
    )

    assert mgr.default_name == "neo4j"
    assert mgr.get_default() is driver


def test_manager_selects_named_driver() -> None:
    neo4j = _StubGraphDriver({GraphFeature.QUERY})
    orientdb = _StubGraphDriver({GraphFeature.QUERY, GraphFeature.INDEXING})

    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(driver=neo4j, capabilities={GraphFeature.QUERY}),
            "orientdb": DriverRegistration(
                driver=orientdb,
                capabilities={GraphFeature.QUERY, GraphFeature.INDEXING},
            ),
        },
        default_name="neo4j",
    )

    assert mgr.get("orientdb") is orientdb


def test_manager_raises_for_unknown_driver() -> None:
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(
                driver=_StubGraphDriver({GraphFeature.QUERY}),
                capabilities={GraphFeature.QUERY},
            ),
        },
        default_name="neo4j",
    )

    with pytest.raises(KeyError):
        mgr.get("unknown")


def test_manager_capability_verification_reports_missing() -> None:
    driver = _StubGraphDriver({GraphFeature.QUERY})
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(driver=driver, capabilities={GraphFeature.QUERY}),
        },
        default_name="neo4j",
    )

    result = mgr.verify_capabilities(
        {"neo4j"},
        {GraphFeature.QUERY, GraphFeature.INDEXING},
    )

    assert result["neo4j"].missing == {GraphFeature.INDEXING}


def test_manager_capability_verification_all_supported() -> None:
    driver = _StubGraphDriver({GraphFeature.QUERY, GraphFeature.INDEXING})
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(driver=driver, capabilities={GraphFeature.QUERY, GraphFeature.INDEXING}),
        },
        default_name="neo4j",
    )

    result = mgr.verify_capabilities(
        {"neo4j"},
        {GraphFeature.QUERY, GraphFeature.INDEXING},
    )

    assert result["neo4j"].missing == set()


def test_manager_query_delegates_to_driver() -> None:
    driver = _StubGraphDriver({GraphFeature.QUERY})
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(driver=driver, capabilities={GraphFeature.QUERY}),
        },
        default_name="neo4j",
    )

    query = GraphQuery(text="MATCH (n) RETURN n", limit=10)
    results = mgr.query(query, driver_name="neo4j")
    assert results == []


def test_manager_query_raises_for_unregistered_driver() -> None:
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(
                driver=_StubGraphDriver({GraphFeature.QUERY}),
                capabilities={GraphFeature.QUERY},
            ),
        },
        default_name="neo4j",
    )

    with pytest.raises(KeyError):
        mgr.query(GraphQuery(text="MATCH (n) RETURN n"), driver_name="unknown")


def test_manager_create_node_delegates() -> None:
    driver = _StubGraphDriver({GraphFeature.PERSISTENCE})
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(driver=driver, capabilities={GraphFeature.PERSISTENCE}),
        },
        default_name="neo4j",
    )

    mgr.create_node(
        node_id="n1",
        labels=["Entity"],
        properties={"name": "test"},
        driver_name="neo4j",
    )


def test_manager_create_edge_delegates() -> None:
    driver = _StubGraphDriver({GraphFeature.PERSISTENCE})
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(driver=driver, capabilities={GraphFeature.PERSISTENCE}),
        },
        default_name="neo4j",
    )

    mgr.create_edge(
        edge_id="e1",
        source_id="n1",
        target_id="n2",
        relation_type="KNOWS",
        properties={},
        driver_name="neo4j",
    )


def test_manager_delete_node_delegates() -> None:
    driver = _StubGraphDriver({GraphFeature.PERSISTENCE})
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(driver=driver, capabilities={GraphFeature.PERSISTENCE}),
        },
        default_name="neo4j",
    )

    mgr.delete_node(node_id="n1", driver_name="neo4j")


def test_manager_delete_edge_delegates() -> None:
    driver = _StubGraphDriver({GraphFeature.PERSISTENCE})
    mgr = GraphManager(
        {
            "neo4j": DriverRegistration(driver=driver, capabilities={GraphFeature.PERSISTENCE}),
        },
        default_name="neo4j",
    )

    mgr.delete_edge(edge_id="e1", driver_name="neo4j")


def test_bind_graph_manager_creates_instance() -> None:
    from af.graph.manager import bind_graph_manager

    driver = _StubGraphDriver({GraphFeature.QUERY})
    mgr = bind_graph_manager(
        {
            "neo4j": DriverRegistration(driver=driver, capabilities={GraphFeature.QUERY}),
        },
        default_name="neo4j",
    )

    assert isinstance(mgr, GraphManager)
    assert mgr.get_default() is driver


def test_graph_driver_protocol_requires_methods() -> None:
    """Verify that GraphDriver protocol expects the right methods."""
    driver = _StubGraphDriver({GraphFeature.QUERY})
    assert hasattr(driver, "verify_capabilities")
    assert hasattr(driver, "query")
    assert hasattr(driver, "create_node")
    assert hasattr(driver, "create_edge")
    assert hasattr(driver, "delete_node")
    assert hasattr(driver, "delete_edge")
