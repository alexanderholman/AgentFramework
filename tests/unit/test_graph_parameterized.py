from __future__ import annotations

import pytest

from af.graph.contracts import (
    GraphPath,
    GraphQuery,
    GraphTransaction,
    GraphSchema,
    GraphTraversal,
)


class TestParameterizedQuery:
    """Acceptance criterion: Queries bind parameters and reject interpolation of untrusted values."""

    def test_query_accepts_parameterized_text_with_placeholders(self) -> None:
        query = GraphQuery(
            text="MATCH (n {id: $nodeId}) RETURN n",
            parameters={"nodeId": "entity-001"},
            limit=10,
        )
        assert query.text == "MATCH (n {id: $nodeId}) RETURN n"
        assert query.parameters == {"nodeId": "entity-001"}

    def test_query_rejects_interpolation_of_untrusted_values(self) -> None:
        with pytest.raises(ValueError, match="interpolation"):
            GraphQuery(
                text="MATCH (n {id: 'entity-001'}) RETURN n",
                parameters={"nodeId": "entity-001"},
                limit=10,
            )

    def test_query_rejects_untrusted_parameter_values(self) -> None:
        with pytest.raises(ValueError, match="untrusted"):
            GraphQuery(
                text="MATCH (n {id: $nodeId}) RETURN n",
                parameters={"nodeId": "'; DROP DATABASE; --"},
                limit=10,
            )

    def test_query_requires_parameters_for_placeholders(self) -> None:
        with pytest.raises(ValueError, match="parameter"):
            GraphQuery(
                text="MATCH (n {id: $nodeId}) RETURN n",
                parameters={},
                limit=10,
            )

    def test_query_allows_non_parameterized_text_without_parameters(self) -> None:
        query = GraphQuery(
            text="MATCH (n) RETURN n",
            limit=10,
        )
        assert query.parameters == {}


class TestBoundedTraversal:
    """Acceptance criterion: Bounded traversal contracts."""

    def test_traversal_validates_depth_bounds(self) -> None:
        traversal = GraphTraversal(
            start_node_id="entity-001",
            max_depth=5,
        )
        assert traversal.start_node_id == "entity-001"
        assert traversal.max_depth == 5

    def test_traversal_rejects_zero_depth(self) -> None:
        with pytest.raises(ValueError):
            GraphTraversal(
                start_node_id="entity-001",
                max_depth=0,
            )

    def test_traversal_rejects_excessive_depth(self) -> None:
        with pytest.raises(ValueError):
            GraphTraversal(
                start_node_id="entity-001",
                max_depth=1001,
            )

    def test_traversal_accepts_direction(self) -> None:
        traversal = GraphTraversal(
            start_node_id="entity-001",
            max_depth=5,
            direction="outgoing",
        )
        assert traversal.direction == "outgoing"

    def test_traversal_accepts_relation_filter(self) -> None:
        traversal = GraphTraversal(
            start_node_id="entity-001",
            max_depth=5,
            relation_types=("KNOWS",),
        )
        assert traversal.relation_types == ("KNOWS",)


class TestGraphPath:
    """Acceptance criterion: Path contracts."""

    def test_path_validates_sequence(self) -> None:
        path = GraphPath(
            path_id="path-001",
            node_ids=("entity-001", "entity-002", "entity-003"),
            edge_ids=("rel-001", "rel-002"),
        )
        assert path.path_id == "path-001"
        assert path.node_ids == ("entity-001", "entity-002", "entity-003")
        assert path.edge_ids == ("rel-001", "rel-002")

    def test_path_rejects_empty_nodes(self) -> None:
        with pytest.raises(ValueError):
            GraphPath(
                path_id="path-001",
                node_ids=(),
                edge_ids=(),
            )

    def test_path_rejects_edge_count_mismatch(self) -> None:
        with pytest.raises(ValueError):
            GraphPath(
                path_id="path-001",
                node_ids=("entity-001", "entity-002"),
                edge_ids=("rel-001", "rel-002"),
            )


class TestGraphTransaction:
    """Acceptance criterion: Transactions and unsupported Community/vendor capabilities are explicit."""

    def test_transaction_validates_id(self) -> None:
        txn = GraphTransaction(
            transaction_id="txn-001",
            driver_name="neo4j",
        )
        assert txn.transaction_id == "txn-001"
        assert txn.driver_name == "neo4j"

    def test_transaction_rejects_empty_id(self) -> None:
        with pytest.raises(ValueError):
            GraphTransaction(
                transaction_id="",
                driver_name="neo4j",
            )

    def test_transaction_rejects_empty_driver(self) -> None:
        with pytest.raises(ValueError):
            GraphTransaction(
                transaction_id="txn-001",
                driver_name="",
            )


class TestGraphSchema:
    """Acceptance criterion: Schema contracts."""

    def test_schema_validates_labels_and_relations(self) -> None:
        schema = GraphSchema(
            schema_id="schema-001",
            labels={"Entity", "Document"},
            relation_types={"KNOWS", "REFERENCES"},
        )
        assert schema.schema_id == "schema-001"
        assert schema.labels == {"Entity", "Document"}
        assert schema.relation_types == {"KNOWS", "REFERENCES"}

    def test_schema_rejects_empty_id(self) -> None:
        with pytest.raises(ValueError):
            GraphSchema(
                schema_id="",
                labels={"Entity"},
                relation_types={"KNOWS"},
            )

    def test_schema_rejects_empty_labels(self) -> None:
        with pytest.raises(ValueError):
            GraphSchema(
                schema_id="schema-001",
                labels=set(),
                relation_types={"KNOWS"},
            )
