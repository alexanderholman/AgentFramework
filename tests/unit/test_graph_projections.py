from dataclasses import replace

import pytest

from af.framework.errors import PlatformError
from af.framework.managers import CapabilitySet
from af.graph.contracts import GraphEdge, GraphNode
from af.graph.projections import GraphProjectionManager, GraphProjectionSnapshot, GraphRelationship
from af.search.contracts import ResourceEnvelope, ResourceKind
from af.search.lifecycle import ResourceIdentity


class Backend:
    """Explicit in-memory protocol double, never a certified graph service."""

    def __init__(self):
        self.nodes, self.edges = {}, {}
        self.fail = False

    def verify_capabilities(self, required):
        return CapabilitySet({"persistence"})

    def create_node(self, node_id, labels, properties):
        if self.fail:
            raise RuntimeError("backend secret")
        self.nodes[node_id] = GraphNode(node_id, labels, dict(properties))

    def create_edge(self, edge_id, source_id, target_id, relation_type, properties):
        self.edges[edge_id] = GraphEdge(
            edge_id, source_id, target_id, relation_type, dict(properties)
        )

    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def get_edge(self, edge_id):
        return self.edges.get(edge_id)


def resource(name, version=1, tenant="tenant"):
    return ResourceEnvelope(
        name,
        ResourceKind.TEXT,
        "source",
        "source:" + name,
        tenant,
        version,
        ("reader",),
        ("sha256:" + "a" * 64,),
        "body " + name,
    )


def snapshot(version=1):
    resources = (resource("one", version), resource("two", version))
    return GraphProjectionSnapshot(
        "snapshot-" + str(version),
        resources,
        (
            GraphRelationship(
                "linked",
                ResourceIdentity.from_resource(resources[0]),
                ResourceIdentity.from_resource(resources[1]),
                "LINK",
                ("sha256:" + "b" * 64,),
            ),
        ),
    )


def test_rebuild_verify_cutover_restart_rollback_retains_provenance(tmp_path):
    backend = Backend()
    path = tmp_path / "graphs.db"
    manager = GraphProjectionManager(backend, path)
    first = manager.rebuild("knowledge", snapshot())
    assert manager.active("knowledge") is None
    manager.cutover(first)
    node = manager.get_resource(
        "knowledge", ResourceIdentity("tenant", "source", "one"), principal="reader"
    )
    assert node and node.properties["provenance"] == ["sha256:" + "a" * 64]
    assert len(backend.edges) == 1
    second = manager.rebuild("knowledge", snapshot(2))
    manager.cutover(second)
    restarted = GraphProjectionManager(backend, path)
    assert restarted.active("knowledge") == second
    assert restarted.rollback("knowledge") == first
    assert restarted.active("knowledge") == first
    with pytest.raises(PlatformError):
        manager.cutover(second)
    with pytest.raises(PlatformError):
        manager.get_resource(
            "knowledge", ResourceIdentity("tenant", "source", "one"), principal="intruder"
        )


def test_failed_or_corrupt_generation_never_publishes(tmp_path):
    backend = Backend()
    manager = GraphProjectionManager(backend, tmp_path / "graphs.db")
    backend.fail = True
    with pytest.raises(PlatformError) as error:
        manager.rebuild("knowledge", snapshot())
    assert "secret" not in str(error.value)
    assert manager.active("knowledge") is None
    backend.fail = False
    build = manager.rebuild("knowledge", snapshot())
    backend.nodes.clear()
    with pytest.raises(PlatformError):
        manager.cutover(build)
    assert manager.active("knowledge") is None


def test_concurrent_build_cutover_is_compare_and_swap_fenced(tmp_path):
    backend = Backend()
    manager = GraphProjectionManager(backend, tmp_path / "graphs.db")
    first = manager.rebuild("knowledge", snapshot())
    competing = manager.rebuild("knowledge", snapshot())
    manager.cutover(first)
    with pytest.raises(PlatformError):
        manager.cutover(competing)
    with pytest.raises(PlatformError):
        manager.rollback("knowledge")


def test_source_snapshot_rejects_missing_cross_tenant_and_unprovenanced_relations():
    one, two = resource("one"), resource("two", tenant="other")
    relation = GraphRelationship(
        "linked",
        ResourceIdentity.from_resource(one),
        ResourceIdentity.from_resource(two),
        "LINK",
        ("sha256:" + "b" * 64,),
    )
    with pytest.raises(ValueError):
        GraphProjectionSnapshot("checkpoint", (one, two), (relation,))
    with pytest.raises(ValueError):
        GraphProjectionSnapshot("checkpoint", (one,), (relation,))
    with pytest.raises(ValueError):
        replace(relation, provenance=())


@pytest.mark.parametrize(
    "changes",
    [
        {"relationship_id": ""},
        {"source": None},
        {"provenance": []},
        {"provenance": ("wrong",)},
        {"provenance": ("sha256:" + "z" * 64,)},
    ],
)
def test_relationship_identity_and_provenance_validation(changes):
    with pytest.raises(ValueError):
        replace(snapshot().relationships[0], **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"resources": ()},
        {"resources": []},
        {"resources": (object(),)},
        {"relationships": []},
        {"relationships": (object(),)},
        {"resources": (resource("one"), resource("one"))},
        {"resources": (resource("one"), resource("two", 2))},
    ],
)
def test_snapshot_requires_a_consistent_bounded_source(changes):
    with pytest.raises(ValueError):
        replace(snapshot(), **changes)


def test_duplicate_relationship_and_disjoint_acl_are_rejected():
    value = snapshot()
    with pytest.raises(ValueError):
        replace(value, relationships=(value.relationships[0],) * 2)
    with pytest.raises(ValueError):
        replace(value, resources=(value.resources[0], replace(value.resources[1], acl=("other",))))


def test_snapshot_manifest_bound_and_resource_attributes():
    one = replace(resource("one"), attributes={f"field_{index}": "a" * 2048 for index in range(64)})
    value = GraphProjectionSnapshot("checkpoint", (one,))
    assert value.payload()["resources"][0]["attributes"]
    large = tuple(replace(one, resource_id=f"resource-{index}") for index in range(150))
    with pytest.raises(ValueError):
        GraphProjectionSnapshot("checkpoint", large)


def test_invalid_handle_no_evidence_no_source_and_missing_resource(tmp_path):
    backend = Backend()
    manager = GraphProjectionManager(backend, tmp_path / "graphs.db")
    identity = ResourceIdentity("tenant", "source", "one")
    assert manager.get_resource("unknown", identity, principal="reader") is None
    with pytest.raises(TypeError):
        manager.rebuild("knowledge", object())
    build = manager.rebuild("knowledge", snapshot())
    with pytest.raises(PlatformError):
        manager.validate(replace(build, source_digest="wrong"))
    manager.cutover(build)
    assert (
        manager.get_resource(
            "knowledge", replace(identity, resource_id="absent"), principal="reader"
        )
        is None
    )
    backend.nodes.clear()
    with pytest.raises(PlatformError):
        manager.get_resource("knowledge", identity, principal="reader")
    backend.verify_capabilities = lambda required: CapabilitySet(())
    with pytest.raises(PlatformError):
        manager.rebuild("denied", snapshot())


def test_corrupt_edge_and_failed_phase_are_never_ready(tmp_path):
    import sqlite3

    backend = Backend()
    path = tmp_path / "graphs.db"
    manager = GraphProjectionManager(backend, path)
    build = manager.rebuild("knowledge", snapshot())
    backend.edges.clear()
    with pytest.raises(PlatformError):
        manager.validate(build)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE graph_generations SET phase='failed'")
    with pytest.raises(PlatformError):
        manager.validate(build)
    with pytest.raises(PlatformError):
        manager.cutover(build)


def test_corrupt_catalog_manifest_is_rejected(tmp_path):
    import sqlite3

    backend = Backend()
    path = tmp_path / "graphs.db"
    manager = GraphProjectionManager(backend, path)
    build = manager.rebuild("knowledge", snapshot())
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE graph_generations SET manifest='{}'")
    with pytest.raises(PlatformError):
        manager.validate(build)


def test_rollback_never_overwrites_a_concurrent_cutover(tmp_path):
    backend = Backend()
    manager = GraphProjectionManager(backend, tmp_path / "graphs.db")
    first = manager.rebuild("knowledge", snapshot())
    manager.cutover(first)
    second = manager.rebuild("knowledge", snapshot(2))
    manager.cutover(second)
    third = manager.rebuild("knowledge", snapshot(3))
    original = backend.get_node

    def concurrent(node_id):
        backend.get_node = original
        manager.cutover(third)
        return original(node_id)

    backend.get_node = concurrent
    with pytest.raises(PlatformError):
        manager.rollback("knowledge")
    assert manager.active("knowledge") == third


def test_rollback_missing_previous_generation_fails_closed(tmp_path):
    import sqlite3

    backend = Backend()
    path = tmp_path / "graphs.db"
    manager = GraphProjectionManager(backend, path)
    first = manager.rebuild("knowledge", snapshot())
    manager.cutover(first)
    second = manager.rebuild("knowledge", snapshot(2))
    manager.cutover(second)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM graph_generations WHERE generation=?", (first.generation_id,)
        )
    with pytest.raises(PlatformError):
        manager.rollback("knowledge")
