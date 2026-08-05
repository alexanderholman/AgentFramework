"""Graph manager with driver registration and selection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from af.framework.managers import CapabilitySet
from af.graph.contracts import GraphFeature, GraphQuery, GraphResult


class GraphDriver(Protocol):
    """Protocol for graph drivers."""

    def verify_capabilities(self, required: set[GraphFeature]) -> CapabilitySet: ...

    def query(self, query: GraphQuery, **kwargs: Any) -> list[GraphResult]: ...

    def create_node(
        self, node_id: str, labels: tuple[str, ...], properties: Mapping[str, Any]
    ) -> None: ...

    def create_edge(
        self,
        edge_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
        properties: Mapping[str, Any],
    ) -> None: ...

    def delete_node(self, node_id: str) -> None: ...

    def delete_edge(self, edge_id: str) -> None: ...


class _DriverEntry:
    """Internal holder for a registered driver and its capabilities."""

    def __init__(self, driver: GraphDriver, capabilities: set[GraphFeature]) -> None:
        self.driver = driver
        self.capabilities = capabilities


class GraphManager:
    """Manage graph drivers through registration and selection."""

    def __init__(
        self,
        registry: dict[str, Any],
        default_name: str | None = None,
    ) -> None:
        if not registry:
            raise ValueError("Graph driver registry must not be empty")

        self._drivers: dict[str, _DriverEntry] = {}
        for name, reg in registry.items():
            self._drivers[name] = _DriverEntry(
                driver=reg.driver,
                capabilities=reg.capabilities,
            )

        self.default_name = default_name

    def get_default(self) -> GraphDriver:
        """Return the default driver."""
        if not self.default_name or self.default_name not in self._drivers:
            raise KeyError(f"No default driver registered: {self.default_name}")
        return self._drivers[self.default_name].driver

    def get(self, name: str) -> GraphDriver:
        """Return a named driver."""
        if name not in self._drivers:
            raise KeyError(name)
        return self._drivers[name].driver

    def verify_capabilities(
        self,
        driver_names: set[str],
        required: set[GraphFeature],
    ) -> dict[str, CapabilitySet]:
        """Verify capabilities for given drivers against required features."""
        result: dict[str, CapabilitySet] = {}
        for name in driver_names:
            if name not in self._drivers:
                raise KeyError(name)
            entry = self._drivers[name]
            result[name] = entry.driver.verify_capabilities(required)
        return result

    def query(self, query: GraphQuery, driver_name: str) -> list[GraphResult]:
        """Execute a graph query using the named driver."""
        return self.get(driver_name).query(query)

    def create_node(
        self,
        node_id: str,
        labels: tuple[str, ...],
        properties: Mapping[str, Any],
        driver_name: str,
    ) -> None:
        """Create a node using the named driver."""
        self.get(driver_name).create_node(node_id, labels, properties)

    def create_edge(
        self,
        edge_id: str,
        source_id: str,
        target_id: str,
        relation_type: str,
        properties: Mapping[str, Any],
        driver_name: str,
    ) -> None:
        """Create an edge using the named driver."""
        self.get(driver_name).create_edge(edge_id, source_id, target_id, relation_type, properties)

    def delete_node(self, node_id: str, driver_name: str) -> None:
        """Delete a node using the named driver."""
        self.get(driver_name).delete_node(node_id)

    def delete_edge(self, edge_id: str, driver_name: str) -> None:
        """Delete an edge using the named driver."""
        self.get(driver_name).delete_edge(edge_id)


def _default_registry() -> dict[str, Any]:
    """Build the default driver registry with Neo4j Community and OrientDB."""
    from types import SimpleNamespace

    from af.graph.drivers.neo4j import Neo4jCommunityDriver
    from af.graph.drivers.orientdb import OrientDBDriver

    neo4j = Neo4jCommunityDriver(uri="bolt://localhost:7687")
    orientdb = OrientDBDriver(uri="remote://localhost:2424", database="graphdb")

    return {
        "neo4j-community": SimpleNamespace(
            driver=neo4j,
            capabilities={GraphFeature.QUERY, GraphFeature.PERSISTENCE},
        ),
        "orientdb": SimpleNamespace(
            driver=orientdb,
            capabilities={GraphFeature.QUERY, GraphFeature.INDEXING, GraphFeature.PERSISTENCE},
        ),
    }


def bind_graph_manager(
    registry: dict[str, Any] | None = None,
    default_name: str | None = None,
) -> GraphManager:
    """Create and return a GraphManager instance.

    When *registry* is ``None``, the default Neo4j Community and OrientDB
    drivers are registered automatically.
    """
    if registry is None:
        registry = _default_registry()
        if default_name is None:
            default_name = "neo4j-community"
    return GraphManager(registry, default_name=default_name)
