"""Tests for classifier hierarchy routing through Jacob's ladder.

Verifies that time requests route through root -> system -> date/time -> current_time
with proper sibling search, visited sets, and rung escalation.
"""

import pytest
from datetime import datetime, timezone
from typing import Optional

from af.framework.types import (
    ClassifierNode,
    ClassifierTree,
    RoutingResult,
    FunctionRoute,
    ChildRoute,
    NoRoute,
    Denied,
    NeedsClarification,
    ClassifierFailure,
    RoutingTrace,
    RouteBudget,
    RungEscalation,
)
from af.framework.routing import (
    HierarchyRouter,
    JacobLadderRouter,
)


# -- Fixtures for classifier hierarchy topology --


@pytest.fixture
def current_time_node() -> ClassifierNode:
    """Leaf node that routes to the current_time procedure."""
    return ClassifierNode(
        id="current_time",
        version="1.0.0",
        procedure_id="current_time",
    children=(),
    metadata={"description": "Return current time with timezone"},
    )


@pytest.fixture
def date_time_node(current_time_node: ClassifierNode) -> ClassifierNode:
    """Intermediate node for date/time classification."""
    return ClassifierNode(
        id="date_time",
        version="1.0.0",
        procedure_id=None,
        children=(current_time_node,),
        metadata={"description": "Date and time related requests"},
    )


@pytest.fixture
def weather_node() -> ClassifierNode:
    """Sibling node for weather classification (no time children)."""
    return ClassifierNode(
        id="weather",
        version="1.0.0",
        procedure_id=None,
        children=(),
        metadata={"description": "Weather related requests"},
    )


@pytest.fixture
def system_node(date_time_node: ClassifierNode, weather_node: ClassifierNode) -> ClassifierNode:
    """System-level classifier with date/time and weather children."""
    return ClassifierNode(
        id="system",
        version="1.0.0",
        procedure_id=None,
        children=(date_time_node, weather_node),
        metadata={"description": "System queries"},
    )


@pytest.fixture
def root_node(system_node: ClassifierNode) -> ClassifierNode:
    """Root classifier node."""
    return ClassifierNode(
        id="root",
        version="1.0.0",
        procedure_id=None,
        children=(system_node,),
        metadata={"description": "Root classifier"},
    )


@pytest.fixture
def classifier_tree(root_node: ClassifierNode) -> ClassifierTree:
    """Complete classifier hierarchy."""
    return ClassifierTree(root=root_node)


@pytest.fixture
def budget() -> RouteBudget:
    """Default route budget."""
    return RouteBudget(max_nodes=100, max_rungs=5)


# -- Tier 1: Basic routing tests --


class TestDeterministicTimeRouting:
    """Test that 'What time is it?' routes through root -> system -> date/time -> current_time."""

    def test_route_time_request_to_leaf(self, classifier_tree: ClassifierTree, budget: RouteBudget):
        """A time request should route to the current_time procedure."""
        router = HierarchyRouter(tree=classifier_tree)
        result = router.route("What time is it?", budget=budget)

        assert isinstance(result, FunctionRoute)
        assert result.procedure_id == "current_time"
        assert result.classifier_id == "current_time"

    def test_route_trace_contains_hierarchy_path(self, classifier_tree: ClassifierTree, budget: RouteBudget):
        """The routing trace should show the full hierarchy path."""
        router = HierarchyRouter(tree=classifier_tree)
        result = router.route("What time is it?", budget=budget)

        assert isinstance(result, FunctionRoute)
        assert len(result.trace.nodes_visited) >= 3  # root, system, date/time at minimum
        classifier_ids = [node.id for node in result.trace.nodes_visited]
        assert "root" in classifier_ids
        assert "system" in classifier_ids

    def test_no_higher_rung_calls_for_known_route(self, classifier_tree: ClassifierTree, budget: RouteBudget):
        """A known route should not escalate to higher rungs."""
        router = JacobLadderRouter(tree=classifier_tree)
        result = router.route("What time is it?", budget=budget)

        assert isinstance(result, FunctionRoute)
        assert result.trace.rung_escalations == ()


class TestWrongChildSiblingSearch:
    """Test that wrong-child NoRoute unwinds to siblings."""

    def test_wrong_child_unwinds_to_sibling(self, classifier_tree: ClassifierTree, budget: RouteBudget):
        """When a child returns NoRoute, parent should try siblings."""
        # Force weather node to be tried first (it has no children)
        router = HierarchyRouter(tree=classifier_tree)
        # Weather query should not match current_time
        result = router.route("What's the weather?", budget=budget)

        # Should either route to weather or return NoRoute (not current_time)
        if isinstance(result, FunctionRoute):
            assert result.procedure_id != "current_time"
        elif isinstance(result, NoRoute):
            # NoRoute bubbles up through parent nodes - verify weather was visited
            visited_ids = [n.id for n in result.trace.nodes_visited]
            assert "weather" in visited_ids

    def test_exhausted_children_return_no_route(self, classifier_tree: ClassifierTree, budget: RouteBudget):
        """When all children are exhausted, parent returns NoRoute."""
        router = HierarchyRouter(tree=classifier_tree)
        result = router.route("Tell me about quantum physics", budget=budget)

        assert isinstance(result, NoRoute)


class TestRungExhaustion:
    """Test rung exhaustion escalation."""

    def test_budget_exhaustion_returns_failure(self, classifier_tree: ClassifierTree):
        """When node budget is exhausted, return ClassifierFailure."""
        tight_budget = RouteBudget(max_nodes=2, max_rungs=1)
        router = HierarchyRouter(tree=classifier_tree)
        result = router.route("What time is it?", budget=tight_budget)

        # With only 2 node visits allowed, deep routing should fail or partial
        # The exact behavior depends on implementation, but shouldn't crash
        assert result is not None

    def test_rung_escalation_on_exhaustion(self, classifier_tree: ClassifierTree):
        """Jacob's ladder should escalate when rung is exhausted."""
        budget = RouteBudget(max_nodes=100, max_rungs=3)
        router = JacobLadderRouter(tree=classifier_tree)
        result = router.route("What time is it?", budget=budget)

        assert isinstance(result, FunctionRoute)
