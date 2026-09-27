"""Finite traversal semantics and native adapter protocol qualification."""

import pytest

from af.framework.errors import PlatformError
from af.graph.contracts import GraphEdge, GraphTraversal
from af.graph.traversal import execute_traversal


def edge(edge_id, source, target, relation="LINK"):
    return GraphEdge(edge_id, source, target, relation, {})


EDGES = (
    edge("ab", "a", "b"),
    edge("bc", "b", "c"),
    edge("ca", "c", "a"),
    edge("bd", "b", "d", "OTHER"),
)


def adjacent(spec, node):
    return [
        item
        for item in EDGES
        if (
            item.source_id == node
            if spec.direction == "outgoing"
            else item.target_id == node
            if spec.direction == "incoming"
            else node in (item.source_id, item.target_id)
        )
        and (not spec.relation_types or item.relation_type in spec.relation_types)
    ]


def test_breadth_first_unique_discovery_paths_obey_depth_direction_and_relations():
    spec = GraphTraversal("a", max_depth=2, relation_types=("LINK",), direction="outgoing")
    calls = []

    def neighbors(node, limit, deadline):
        calls.append((node, limit, deadline))
        return adjacent(spec, node)

    paths = execute_traversal(spec, neighbors)
    assert [(path.node_ids, path.edge_ids) for path in paths] == [
        (("a", "b"), ("ab",)),
        (("a", "b", "c"), ("ab", "bc")),
    ]
    assert len({path.path_id for path in paths}) == 2
    assert [call[0] for call in calls] == ["a", "b"]
    assert all(0 < call[2] <= 5000 for call in calls)


@pytest.mark.parametrize("direction,expected", [("any", {"b", "c", "d"}), ("incoming", {"b", "c"})])
def test_cycles_terminate_without_duplicate_destinations(direction, expected):
    spec = GraphTraversal("a", max_depth=1000, direction=direction)
    paths = execute_traversal(spec, lambda node, limit, deadline: adjacent(spec, node))
    assert {path.node_ids[-1] for path in paths} == expected
    assert all(len(set(path.node_ids)) == len(path.node_ids) for path in paths)


def test_result_limit_stops_and_edge_budget_fails_without_partial_success():
    spec = GraphTraversal("a", max_results=1)
    assert len(execute_traversal(spec, lambda *args: [edge("ab", "a", "b")])) == 1
    with pytest.raises(PlatformError) as caught:
        execute_traversal(
            GraphTraversal("a", max_edges=1),
            lambda *args: [edge("ab", "a", "b"), edge("ac", "a", "c")],
        )
    assert caught.value.stage == "graph.traversal.edge-budget"


def test_deadline_checked_after_backend_returns():
    times = iter([0.0, 0.0, 1.0])
    with pytest.raises(PlatformError) as caught:
        execute_traversal(
            GraphTraversal("a", deadline_ms=10), lambda *args: [], clock=lambda: next(times)
        )
    assert caught.value.stage == "graph.traversal.deadline"


@pytest.mark.parametrize(
    "bad", [edge("bc", "b", "c"), edge("ba", "b", "a"), edge("ab", "a", "b", "WRONG")]
)
def test_backend_cannot_return_foreign_direction_or_relation(bad):
    with pytest.raises(PlatformError):
        execute_traversal(
            GraphTraversal("a", direction="outgoing", relation_types=("LINK",)), lambda *args: [bad]
        )


@pytest.mark.parametrize("kind", ["neo4j", "orientdb"])
@pytest.mark.parametrize(
    "direction,relations", [("outgoing", ("LINK",)), ("incoming", ()), ("any", ())]
)
def test_native_adapters_execute_each_hop_with_bound_values(
    kind, direction, relations, monkeypatch
):
    from tests.unit.test_neo4j_community import Client as NeoClient
    from tests.unit.test_neo4j_community import Transaction
    from tests.unit.test_orientdb_driver import Client as OrientClient
    from tests.unit.test_orientdb_driver import Response

    from af.framework.managers import CapabilitySet
    from af.graph.contracts import GraphFeature
    from af.graph.drivers.neo4j import Neo4jCommunityDriver
    from af.graph.drivers.orientdb import OrientDBDriver
    from af.graph.manager import GraphDriverRegistration, GraphManager

    spec = GraphTraversal("a", max_depth=3, direction=direction, relation_types=relations)

    def rows(node):
        return [
            {
                "edge_id": item.edge_id,
                "source_id": item.source_id,
                "target_id": item.target_id,
                "relation_type": item.relation_type,
            }
            for item in adjacent(spec, node)
        ]

    if kind == "neo4j":
        client = NeoClient()

        def run(self, text, parameters):
            self.client.calls.append((text, dict(parameters)))
            return iter(rows(parameters["node_id"]))

        monkeypatch.setattr(Transaction, "run", run)
        queue = Neo4jCommunityDriver(
            "bolt://localhost",
            client=client,
            capability_verifier=lambda: CapabilitySet({"traversal"}),
        )
    else:
        client = OrientClient()

        def post(self, url, *, json, timeout):
            self.calls.append((url, json, timeout))
            return Response(rows(json["parameters"]["node_id"]))

        monkeypatch.setattr(OrientClient, "post", post)
        queue = OrientDBDriver(
            "https://localhost",
            "graphdb",
            client=client,
            capability_verifier=lambda: CapabilitySet({"traversal"}),
        )
    manager = GraphManager({"graph": GraphDriverRegistration(queue, {GraphFeature.TRAVERSAL})})
    paths = manager.traverse(spec, "graph")
    expected = execute_traversal(spec, lambda node, limit, deadline: adjacent(spec, node))
    assert paths == expected
    assert client.calls
    if kind == "neo4j":
        assert all("LIMIT $limit" in text and "$node_id" in text for text, params in client.calls)
        assert all(0 < timeout <= 5 for timeout in client.timeouts)
        if relations:
            assert all(params["relation_types"] == list(relations) for text, params in client.calls)
    else:
        assert all(
            "TIMEOUT" in payload["command"] and ":node_id" in payload["command"]
            for url, payload, timeout in client.calls
        )
        assert all(0 < timeout <= 5 for url, payload, timeout in client.calls)
        if relations:
            assert all(
                payload["parameters"]["relation_types"] == list(relations)
                for url, payload, timeout in client.calls
            )
    with pytest.raises(TypeError):
        queue.traverse(object())


def test_traversal_requires_both_evidence_and_implemented_operation():
    from af.framework.managers import CapabilitySet
    from af.graph.contracts import GraphFeature
    from af.graph.drivers.neo4j import Neo4jCommunityDriver
    from af.graph.manager import GraphDriverRegistration, GraphManager

    no_client = Neo4jCommunityDriver("bolt://localhost")
    manager = GraphManager({"graph": GraphDriverRegistration(no_client, {GraphFeature.TRAVERSAL})})
    with pytest.raises(PlatformError):
        manager.traverse(GraphTraversal("a"), "graph")

    class Liar:
        def verify_capabilities(self, required):
            return CapabilitySet({"traversal"})

    manager = GraphManager({"graph": GraphDriverRegistration(Liar(), {GraphFeature.TRAVERSAL})})
    with pytest.raises(PlatformError):
        manager.traverse(GraphTraversal("a"), "graph")


@pytest.mark.parametrize(
    "options",
    [
        {"max_depth": True},
        {"max_results": 0},
        {"max_results": 10001},
        {"max_edges": 0},
        {"deadline_ms": 0},
        {"deadline_ms": float("nan")},
        {"relation_types": ["LINK"]},
        {"direction": "sideways"},
    ],
)
def test_traversal_rejects_ambiguous_or_unbounded_inputs(options):
    with pytest.raises(ValueError):
        GraphTraversal("a", **options)


def test_deadline_before_call_and_exact_edge_budget_and_invalid_projection():
    from af.graph.traversal import edges_from_rows

    times = iter([0.0, 10.0])
    with pytest.raises(PlatformError):
        execute_traversal(GraphTraversal("a"), lambda *args: [], clock=lambda: next(times))
    with pytest.raises(PlatformError):
        execute_traversal(GraphTraversal("a", max_edges=1), lambda *args: [edge("ab", "a", "b")])
    with pytest.raises(PlatformError):
        edges_from_rows([{"edge_id": "missing-fields"}])
    with pytest.raises(TypeError):
        execute_traversal(object(), lambda *args: [])
