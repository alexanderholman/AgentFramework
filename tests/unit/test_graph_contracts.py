from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from af.graph.contracts import (
    GraphEdge,
    GraphFeature,
    GraphNode,
    GraphPattern,
    GraphQuery,
    GraphResult,
)


def test_graph_node_validates_identity_and_properties() -> None:
    node = GraphNode(
        node_id="entity-001",
        labels=("Entity",),
        properties={"name": "test"},
    )

    assert node.node_id == "entity-001"
    assert node.labels == ("Entity",)
    assert node.properties["name"] == "test"

    with pytest.raises(ValueError):
        GraphNode(node_id="", labels=("Entity",), properties={})
    with pytest.raises(ValueError):
        GraphNode(node_id="entity-001", labels=(), properties={})
    with pytest.raises(ValueError):
        GraphNode(node_id="entity-001", labels=("a" * 65,), properties={})
    with pytest.raises(ValueError):
        GraphNode(node_id="entity-001", labels=("Entity",), properties={"k": "v" * 100_001})


def test_graph_edge_validates_source_target_and_properties() -> None:
    edge = GraphEdge(
        edge_id="rel-001",
        source_id="entity-001",
        target_id="entity-002",
        relation_type="KNOWS",
        properties={"since": "2024"},
    )

    assert edge.edge_id == "rel-001"
    assert edge.source_id == "entity-001"
    assert edge.target_id == "entity-002"
    assert edge.relation_type == "KNOWS"
    assert edge.properties["since"] == "2024"

    with pytest.raises(ValueError):
        GraphEdge(
            edge_id="",
            source_id="entity-001",
            target_id="entity-002",
            relation_type="KNOWS",
            properties={},
        )
    with pytest.raises(ValueError):
        GraphEdge(
            edge_id="rel-001",
            source_id="entity-001",
            target_id="entity-001",
            relation_type="KNOWS",
            properties={},
        )
    with pytest.raises(ValueError):
        GraphEdge(
            edge_id="rel-001",
            source_id="entity-001",
            target_id="entity-002",
            relation_type="",
            properties={},
        )
    with pytest.raises(ValueError):
        GraphEdge(
            edge_id="rel-001",
            source_id="entity-001",
            target_id="entity-002",
            relation_type="a" * 65,
            properties={},
        )


def test_graph_pattern_validates_path_constraints() -> None:
    pattern = GraphPattern(
        pattern_id="path-001",
        node_ids=("entity-001", "entity-002"),
        edge_ids=("rel-001",),
    )

    assert pattern.pattern_id == "path-001"
    assert pattern.node_ids == ("entity-001", "entity-002")
    assert pattern.edge_ids == ("rel-001",)

    with pytest.raises(ValueError):
        GraphPattern(
            pattern_id="",
            node_ids=("entity-001",),
            edge_ids=(),
        )
    with pytest.raises(ValueError):
        GraphPattern(
            pattern_id="path-001",
            node_ids=(),
            edge_ids=(),
        )
    with pytest.raises(ValueError):
        GraphPattern(
            pattern_id="path-001",
            node_ids=("entity-001",),
            edge_ids=("rel-001",),
        )


def test_graph_query_validates_text_and_policy() -> None:
    query = GraphQuery(
        text="MATCH (n)-[r]->(m) RETURN n, r, m",
        limit=10,
    )

    assert query.text == "MATCH (n)-[r]->(m) RETURN n, r, m"
    assert query.limit == 10
    assert query.deadline_ms == 5_000

    with pytest.raises(ValueError):
        GraphQuery(text="", limit=10)
    with pytest.raises(ValueError):
        GraphQuery(text="MATCH (n) RETURN n", limit=0)
    with pytest.raises(ValueError):
        GraphQuery(text="MATCH (n) RETURN n", limit=10_001)
    with pytest.raises(ValueError):
        GraphQuery(text="MATCH (n) RETURN n", deadline_ms=0)
    with pytest.raises(ValueError):
        GraphQuery(text="MATCH (n) RETURN n", deadline_ms=120_001)


def test_graph_result_validates_score_and_rank() -> None:
    result = GraphResult(
        node_id="entity-001",
        labels=("Entity",),
        score=1.0,
        rank=1,
    )

    assert result.node_id == "entity-001"
    assert result.labels == ("Entity",)
    assert result.score == 1.0
    assert result.rank == 1

    with pytest.raises(ValueError):
        GraphResult(node_id="", labels=("Entity",), score=1.0, rank=1)
    with pytest.raises(ValueError):
        GraphResult(node_id="entity-001", labels=("Entity",), score=-1.0, rank=1)
    with pytest.raises(ValueError):
        GraphResult(node_id="entity-001", labels=("Entity",), score=1.0, rank=0)
    with pytest.raises(ValueError):
        GraphResult(node_id="entity-001", labels=("Entity",), score=1.0, rank=10_001)


def test_graph_feature_enum_has_expected_values() -> None:
    assert GraphFeature.QUERY.value == "query"
    assert GraphFeature.INDEXING.value == "indexing"
    assert GraphFeature.PERSISTENCE.value == "persistence"
    assert GraphFeature.CANCELLATION.value == "cancellation"


def test_graph_contracts_are_immutable() -> None:
    node = GraphNode(
        node_id="entity-001",
        labels=("Entity",),
        properties={"name": "test"},
    )

    with pytest.raises(FrozenInstanceError):
        node.node_id = "entity-002"  # type: ignore[misc]

    edge = GraphEdge(
        edge_id="rel-001",
        source_id="entity-001",
        target_id="entity-002",
        relation_type="KNOWS",
        properties={},
    )

    with pytest.raises(FrozenInstanceError):
        edge.edge_id = "rel-002"  # type: ignore[misc]


@pytest.mark.parametrize("score", [float("nan"), float("inf"), True])
def test_graph_result_rejects_nonfinite_or_boolean_score(score):
    with pytest.raises(ValueError):
        GraphResult("node", ("Entity",), score, 1)


@pytest.mark.parametrize("rank", [True, 1.5])
def test_graph_result_requires_integer_rank(rank):
    with pytest.raises(ValueError):
        GraphResult("node", ("Entity",), 1.0, rank)
