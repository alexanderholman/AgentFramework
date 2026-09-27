"""Durable generation catalog for rebuilding graph projections from source snapshots.

Graph writes build an unpublished generation. Read-back verification precedes a
SQLite compare-and-swap alias change. There is no cross-store transaction and no
claim that projected graph data is an authoritative source.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import CapabilitySet
from af.graph.contracts import GraphEdge, GraphFeature, GraphNode, _identifier, _label
from af.search.contracts import ResourceEnvelope
from af.search.lifecycle import ResourceIdentity


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _failure(stage: str) -> PlatformError:
    return PlatformError(PlatformReason.INTEGRITY_FAILURE, stage=f"graph.projection.{stage}")


@dataclass(frozen=True, slots=True)
class GraphRelationship:
    relationship_id: str
    source: ResourceIdentity
    target: ResourceIdentity
    relation_type: str
    provenance: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.relationship_id, "relationship id")
        _label(self.relation_type)
        if (
            not isinstance(self.source, ResourceIdentity)
            or not isinstance(self.target, ResourceIdentity)
            or self.source == self.target
        ):
            raise ValueError("graph relationship requires distinct resource identities")
        if type(self.provenance) is not tuple or not self.provenance or len(self.provenance) > 256:
            raise ValueError("graph relationship requires bounded immutable provenance")
        for digest in self.provenance:
            if (
                type(digest) is not str
                or len(digest) != 71
                or not digest.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in digest[7:])
            ):
                raise ValueError("graph relationship provenance must contain SHA-256 digests")


def _resource(resource: ResourceEnvelope) -> dict[str, object]:
    return {
        "resource_id": resource.resource_id,
        "kind": resource.kind.value,
        "source_id": resource.source_id,
        "authoritative_locator": resource.authoritative_locator,
        "tenant_id": resource.tenant_id,
        "projection_version": resource.projection_version,
        "acl": list(resource.acl),
        "provenance": list(resource.provenance),
        "text_digest": "sha256:" + hashlib.sha256(resource.text.encode()).hexdigest(),
        "attributes": [
            json.dumps([key, value], separators=(",", ":"))
            for key, value in sorted(resource.attributes.items())
        ],
    }


@dataclass(frozen=True, slots=True)
class GraphProjectionSnapshot:
    """One caller-verified authoritative snapshot, never inferred from graph content."""

    checkpoint: str
    resources: tuple[ResourceEnvelope, ...]
    relationships: tuple[GraphRelationship, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.checkpoint, "source checkpoint", max_len=256)
        if (
            type(self.resources) is not tuple
            or not self.resources
            or len(self.resources) > 1000
            or any(not isinstance(item, ResourceEnvelope) for item in self.resources)
        ):
            raise ValueError("graph snapshot requires 1..1000 typed resources")
        if (
            type(self.relationships) is not tuple
            or len(self.relationships) > 5000
            or any(not isinstance(item, GraphRelationship) for item in self.relationships)
        ):
            raise ValueError("graph snapshot relationships must be bounded and immutable")
        identities = {
            ResourceIdentity.from_resource(resource): resource for resource in self.resources
        }
        if (
            len(identities) != len(self.resources)
            or len({r.projection_version for r in self.resources}) != 1
        ):
            raise ValueError("graph snapshot resource identities/version must be consistent")
        if len({r.relationship_id for r in self.relationships}) != len(self.relationships):
            raise ValueError("graph snapshot relationship identities must be unique")
        for relation in self.relationships:
            if relation.source not in identities or relation.target not in identities:
                raise ValueError("graph relationship endpoint is absent from the source snapshot")
            if relation.source.tenant_id != relation.target.tenant_id or not set(
                identities[relation.source].acl
            ) & set(identities[relation.target].acl):
                raise ValueError(
                    "graph relationships cannot cross tenant or disjoint ACL boundaries"
                )
        if len(json.dumps(self.payload()).encode()) > 16 * 1_048_576:
            raise ValueError("graph snapshot exceeds its manifest bound")

    def payload(self) -> dict[str, object]:
        return {
            "checkpoint": self.checkpoint,
            "resources": [_resource(item) for item in self.resources],
            "relationships": [asdict(item) for item in self.relationships],
        }


@dataclass(frozen=True, slots=True)
class GraphGeneration:
    logical_name: str
    generation_id: str
    base_generation: str | None
    base_revision: int
    source_digest: str


class ProjectionGraphDriver(Protocol):
    def verify_capabilities(self, required: set[GraphFeature]) -> CapabilitySet: ...
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
    def get_node(self, node_id: str) -> GraphNode | None: ...
    def get_edge(self, edge_id: str) -> GraphEdge | None: ...


class GraphProjectionManager:
    """Operator-owned rebuild/cutover/rollback with durable alias fencing.

    Reserve a private graph namespace and protect the SQLite catalog. Build input
    and calls are trusted operator actions. Use get_resource for an ACL-checked
    active point lookup; raw native graph access requires separate authorization.
    """

    def __init__(self, driver: ProjectionGraphDriver, catalog: Path) -> None:
        self._driver, self._catalog = driver, catalog
        with self._transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS graph_generations (
                    generation TEXT PRIMARY KEY, logical_name TEXT NOT NULL,
                    base_generation TEXT, base_revision INTEGER NOT NULL,
                    source_digest TEXT NOT NULL, phase TEXT NOT NULL,
                    manifest TEXT NOT NULL, manifest_digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS graph_aliases (
                    logical_name TEXT PRIMARY KEY, generation TEXT, revision INTEGER NOT NULL
                );
            """)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._catalog, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
        finally:
            connection.close()

    def _capability(self) -> None:
        if not self._driver.verify_capabilities({GraphFeature.PERSISTENCE}).supports(
            {"persistence"}
        ):
            raise PlatformError(
                PlatformReason.CAPABILITY_UNAVAILABLE, stage="graph.projection.capability"
            )

    @staticmethod
    def _generation(row: sqlite3.Row) -> GraphGeneration:
        return GraphGeneration(
            row["logical_name"],
            row["generation"],
            row["base_generation"],
            row["base_revision"],
            row["source_digest"],
        )

    def _load(self, generation: GraphGeneration) -> tuple[str, dict[str, Any]]:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM graph_generations WHERE generation=?", (generation.generation_id,)
            ).fetchone()
            if row is None or self._generation(row) != generation:
                raise _failure("handle")
            manifest = json.loads(row["manifest"])
            if not isinstance(manifest, dict) or _digest(manifest) != row["manifest_digest"]:
                raise _failure("manifest")
            return row["phase"], manifest

    @staticmethod
    def _manifest(generation_id: str, snapshot: GraphProjectionSnapshot) -> dict[str, Any]:
        nodes, edges = [], []
        identities = {}
        resources = {}
        for resource in snapshot.resources:
            identity = ResourceIdentity.from_resource(resource)
            node_id = _digest([generation_id, asdict(identity)])
            identities[identity], resources[identity] = node_id, resource
            nodes.append(
                asdict(
                    GraphNode(
                        node_id, ("Resource",), {**_resource(resource), "generation": generation_id}
                    )
                )
            )
        for relation in snapshot.relationships:
            properties = {
                "generation": generation_id,
                "relationship_id": relation.relationship_id,
                "provenance": list(relation.provenance),
                "tenant_id": relation.source.tenant_id,
                "acl": sorted(
                    set(resources[relation.source].acl) & set(resources[relation.target].acl)
                ),
            }
            edges.append(
                asdict(
                    GraphEdge(
                        _digest([generation_id, relation.relationship_id]),
                        identities[relation.source],
                        identities[relation.target],
                        relation.relation_type,
                        properties,
                    )
                )
            )
        return {"checkpoint": snapshot.checkpoint, "nodes": nodes, "edges": edges}

    def rebuild(self, logical_name: str, snapshot: GraphProjectionSnapshot) -> GraphGeneration:
        _identifier(logical_name, "logical graph name")
        if not isinstance(snapshot, GraphProjectionSnapshot):
            raise TypeError("graph rebuild requires an authoritative snapshot")
        self._capability()
        token = secrets.token_hex(32)
        manifest = self._manifest(token, snapshot)
        with self._transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO graph_aliases VALUES (?,NULL,0)", (logical_name,)
            )
            alias = connection.execute(
                "SELECT * FROM graph_aliases WHERE logical_name=?", (logical_name,)
            ).fetchone()
            build = GraphGeneration(
                logical_name,
                token,
                alias["generation"],
                alias["revision"],
                _digest(snapshot.payload()),
            )
            connection.execute(
                "INSERT INTO graph_generations VALUES (?,?,?,?,?,?,?,?)",
                (
                    token,
                    logical_name,
                    build.base_generation,
                    build.base_revision,
                    build.source_digest,
                    "building",
                    json.dumps(manifest, sort_keys=True),
                    _digest(manifest),
                ),
            )
        try:
            for node in manifest["nodes"]:
                self._driver.create_node(node["node_id"], tuple(node["labels"]), node["properties"])
            for edge in manifest["edges"]:
                self._driver.create_edge(
                    edge["edge_id"],
                    edge["source_id"],
                    edge["target_id"],
                    edge["relation_type"],
                    edge["properties"],
                )
            self.validate(build)
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE graph_generations SET phase='ready' WHERE generation=?", (token,)
                )
            return build
        except Exception as error:
            with self._transaction() as connection:
                connection.execute(
                    "UPDATE graph_generations SET phase='failed' WHERE generation=?", (token,)
                )
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="graph.projection.rebuild",
                protected_cause=error,
            ) from None

    def validate(self, generation: GraphGeneration) -> None:
        self._capability()
        phase, manifest = self._load(generation)
        if phase == "failed":
            raise _failure("phase")
        for value in manifest["nodes"]:
            expected = GraphNode(value["node_id"], tuple(value["labels"]), value["properties"])
            if self._driver.get_node(expected.node_id) != expected:
                raise _failure("node")
        for value in manifest["edges"]:
            expected_edge = GraphEdge(**value)
            if self._driver.get_edge(expected_edge.edge_id) != expected_edge:
                raise _failure("edge")

    def active(self, logical_name: str) -> GraphGeneration | None:
        _identifier(logical_name, "logical graph name")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT g.* FROM graph_generations g JOIN graph_aliases a "
                "ON g.generation=a.generation WHERE a.logical_name=?",
                (logical_name,),
            ).fetchone()
            return None if row is None else self._generation(row)

    def cutover(self, generation: GraphGeneration) -> None:
        phase, _ = self._load(generation)
        if phase != "ready":
            raise _failure("phase")
        self.validate(generation)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE graph_aliases SET generation=?,revision=revision+1 "
                "WHERE logical_name=? AND generation IS ? AND revision=?",
                (
                    generation.generation_id,
                    generation.logical_name,
                    generation.base_generation,
                    generation.base_revision,
                ),
            ).rowcount
            if changed != 1:
                raise _failure("fence")
            connection.execute(
                "UPDATE graph_generations SET phase='active' WHERE generation=?",
                (generation.generation_id,),
            )
            connection.execute(
                "UPDATE graph_generations SET phase='retired' WHERE generation=?",
                (generation.base_generation,),
            )

    def rollback(self, logical_name: str) -> GraphGeneration:
        current = self.active(logical_name)
        if current is None or current.base_generation is None:
            raise _failure("rollback")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM graph_generations WHERE generation=?", (current.base_generation,)
            ).fetchone()
            if row is None:
                raise _failure("rollback")
            previous = self._generation(row)
            alias = connection.execute(
                "SELECT revision FROM graph_aliases WHERE logical_name=?", (logical_name,)
            ).fetchone()
            revision = alias["revision"]
        self.validate(previous)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE graph_aliases SET generation=?,revision=revision+1 "
                "WHERE logical_name=? AND generation=? AND revision=?",
                (previous.generation_id, logical_name, current.generation_id, revision),
            ).rowcount
            if changed != 1:
                raise _failure("fence")
            connection.execute(
                "UPDATE graph_generations SET phase='active' WHERE generation=?",
                (previous.generation_id,),
            )
            connection.execute(
                "UPDATE graph_generations SET phase='retired' WHERE generation=?",
                (current.generation_id,),
            )
        return previous

    def get_resource(
        self, logical_name: str, identity: ResourceIdentity, *, principal: str
    ) -> GraphNode | None:
        generation = self.active(logical_name)
        if generation is None:
            return None
        _, manifest = self._load(generation)
        node_id = _digest([generation.generation_id, asdict(identity)])
        for value in manifest["nodes"]:
            if value["node_id"] == node_id:
                if principal not in value["properties"]["acl"]:
                    raise PlatformError(
                        PlatformReason.AUTHORIZATION_DENIED, stage="graph.projection.acl"
                    )
                self._capability()
                result = self._driver.get_node(node_id)
                if result != GraphNode(node_id, tuple(value["labels"]), value["properties"]):
                    raise _failure("node")
                return result
        return None
