"""Graph manager with driver registration and selection."""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True, repr=False)
class GraphDriverRegistration:
    """Explicit graph-driver registration for operator-owned composition."""

    driver: GraphDriver
    capabilities: Set[GraphFeature]

    def __post_init__(self) -> None:
        capabilities = frozenset(self.capabilities)
        if not all(isinstance(feature, GraphFeature) for feature in capabilities):
            raise ValueError("graph driver capabilities must contain GraphFeature values")
        object.__setattr__(self, "capabilities", capabilities)


class _DriverEntry:
    """Internal holder for a registered driver and its capabilities."""

    def __init__(self, driver: GraphDriver, capabilities: Set[GraphFeature]) -> None:
        self.driver = driver
        self.capabilities = capabilities


class GraphManager:
    """Manage graph drivers through registration and selection."""

    def __init__(
        self,
        registry: Mapping[str, GraphDriverRegistration],
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


def bind_graph_manager(
    registry: Mapping[str, GraphDriverRegistration],
    default_name: str | None = None,
) -> GraphManager:
    """Create a manager from an explicit, operator-owned driver registry."""
    return GraphManager(registry, default_name=default_name)
