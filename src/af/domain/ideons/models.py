"""Immutable value objects for authoritative Ideon graphs.

The objects in this module deliberately contain no storage or framework concerns.
Collections are tuples and set-like graph collections are normalised at construction
time, making every value deeply immutable and suitable for canonical encoding.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from string import hexdigits

from .numeric import integer_to_decimal, natural_to_hex


class IdeonKind(StrEnum):
    """The high-level identity class of an Ideon."""

    PRIME = "prime"
    COMPOSITE = "composite"
    OPEN = "open"
    RELATION = "relation"
    CONTEXT = "context"


class NodeKind(StrEnum):
    """Portable node categories shared by dictionary and inquiry layers."""

    ENTITY = "entity"
    EVENT = "event"
    VALUE = "value"
    FORM = "form"
    SENSE = "sense"
    RELATION = "relation"
    CONTEXT = "context"
    CANDIDATE_SET = "candidate-set"


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def _require_positive_key(value: int, field_name: str = "key") -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a natural number greater than zero")


def _normalise_strings(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalised = tuple(values)
    for value in normalised:
        _require_text(value, field_name)
    if len(set(normalised)) != len(normalised):
        raise ValueError(f"{field_name} must not contain duplicates")
    return tuple(sorted(normalised))


@dataclass(frozen=True, slots=True, order=True)
class IdeonRef:
    """An exact address: stable semantic key plus immutable graph revision."""

    key: int
    revision: str

    def __post_init__(self) -> None:
        _require_positive_key(self.key)
        if (
            not isinstance(self.revision, str)
            or len(self.revision) != 64
            or any(character not in hexdigits for character in self.revision)
            or self.revision != self.revision.lower()
        ):
            raise ValueError("revision must be a lowercase hexadecimal SHA-256 digest")


type PropertyValue = str | int | bool | IdeonRef | None


@dataclass(frozen=True, slots=True)
class Property:
    """A deterministic, scalar or reference-valued graph property.

    Exact decimal, rational, interval, or qualified numeric values should be stored
    as canonical strings. Binary floating-point values are intentionally excluded.
    """

    name: str
    value: PropertyValue

    def __post_init__(self) -> None:
        _require_text(self.name, "property name")
        if not (
            self.value is None
            or type(self.value) in (str, int, bool)
            or isinstance(self.value, IdeonRef)
        ):
            raise TypeError("property value must be str, int, bool, None, or an IdeonRef")


def _normalise_properties(values: tuple[Property, ...], field_name: str) -> tuple[Property, ...]:
    normalised = tuple(values)
    if any(not isinstance(value, Property) for value in normalised):
        raise TypeError(f"{field_name} must contain Property values")
    names = tuple(value.name for value in normalised)
    if len(set(names)) != len(names):
        raise ValueError(f"{field_name} property names must be unique")
    return tuple(sorted(normalised, key=lambda value: value.name))


def _property_value_sort_key(value: PropertyValue) -> tuple[str, str, str]:
    if isinstance(value, IdeonRef):
        return ("ideon-ref", natural_to_hex(value.key), value.revision)
    if value is None:
        return ("null", "", "")
    if isinstance(value, bool):
        return ("bool", "1" if value else "0", "")
    if isinstance(value, int):
        return ("int", integer_to_decimal(value), "")
    return ("str", value, "")


def _properties_sort_key(
    values: tuple[Property, ...],
) -> tuple[tuple[str, tuple[str, str, str]], ...]:
    return tuple((value.name, _property_value_sort_key(value.value)) for value in values)


@dataclass(frozen=True, slots=True, order=True)
class IdentityRole:
    """An identity-bearing relation role filled by another stable Ideon key."""

    role: str
    key: int

    def __post_init__(self) -> None:
        _require_text(self.role, "identity role")
        _require_positive_key(self.key, "identity role key")


@dataclass(frozen=True, slots=True)
class IdeonIdentity:
    """The fields which decide *which Ideon* a graph revision describes.

    ``roles`` and ``order`` deliberately preserve their supplied sequence. Context
    behaves as a mapping and is sorted by property name. Consequently role, order,
    namespace, ruleset, and identity-bearing context all participate in the stable
    identity while non-identity graph evidence may evolve between revisions.
    """

    namespace: str
    ruleset: str
    name: str
    kind: IdeonKind = IdeonKind.COMPOSITE
    roles: tuple[IdentityRole, ...] = ()
    order: tuple[int, ...] = ()
    context: tuple[Property, ...] = ()
    roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.namespace, "namespace")
        _require_text(self.ruleset, "ruleset")
        _require_text(self.name, "identity name")
        if not isinstance(self.kind, IdeonKind):
            raise TypeError("kind must be an IdeonKind")

        roles = tuple(self.roles)
        if any(not isinstance(role, IdentityRole) for role in roles):
            raise TypeError("roles must contain IdentityRole values")
        if len(set(roles)) != len(roles):
            raise ValueError("roles must not contain duplicate bindings")
        object.__setattr__(self, "roles", roles)

        order = tuple(self.order)
        for key in order:
            _require_positive_key(key, "composition order key")
        object.__setattr__(self, "order", order)
        object.__setattr__(self, "context", _normalise_properties(self.context, "context"))

        roots = tuple(self.roots)
        for root in roots:
            _require_text(root, "root node id")
        if len(set(roots)) != len(roots):
            raise ValueError("roots must not contain duplicates")
        object.__setattr__(self, "roots", roots)


@dataclass(frozen=True, slots=True)
class IdeonNode:
    """A local vertex in an authoritative Ideon graph."""

    node_id: str
    kind: NodeKind = NodeKind.ENTITY
    ideon: IdeonRef | None = None
    type_ref: IdeonRef | None = None
    properties: tuple[Property, ...] = ()
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.node_id, "node id")
        if not isinstance(self.kind, NodeKind):
            raise TypeError("node kind must be a NodeKind")
        if self.ideon is not None and not isinstance(self.ideon, IdeonRef):
            raise TypeError("node ideon must be an IdeonRef or None")
        if self.type_ref is not None and not isinstance(self.type_ref, IdeonRef):
            raise TypeError("node type_ref must be an IdeonRef or None")
        object.__setattr__(
            self,
            "properties",
            _normalise_properties(self.properties, "node properties"),
        )
        object.__setattr__(
            self, "provenance", _normalise_strings(self.provenance, "node provenance")
        )


type Relation = str | IdeonRef


def _validate_relation(value: Relation) -> None:
    if isinstance(value, str):
        _require_text(value, "relation")
    elif not isinstance(value, IdeonRef):
        raise TypeError("relation must be a non-empty string or IdeonRef")


def _relation_sort_key(value: Relation) -> tuple[str, str, str]:
    if isinstance(value, str):
        return ("label", value, "")
    return ("ref", natural_to_hex(value.key), value.revision)


@dataclass(frozen=True, slots=True, order=True)
class RoleBinding:
    """A named participant in an n-ary relation or hyperedge."""

    role: str
    node_id: str

    def __post_init__(self) -> None:
        _require_text(self.role, "relation role")
        _require_text(self.node_id, "role node id")


@dataclass(frozen=True, slots=True)
class IdeonEdge:
    """Compact directed binary relation representation."""

    source: str
    relation: Relation
    target: str
    source_role: str = "origin"
    target_role: str = "destination"
    context: tuple[Property, ...] = ()
    weight: str | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.source, "edge source")
        _validate_relation(self.relation)
        _require_text(self.target, "edge target")
        _require_text(self.source_role, "edge source role")
        _require_text(self.target_role, "edge target role")
        if self.weight is not None:
            _require_text(self.weight, "edge weight")
        object.__setattr__(self, "context", _normalise_properties(self.context, "edge context"))
        object.__setattr__(
            self, "provenance", _normalise_strings(self.provenance, "edge provenance")
        )

    def sort_key(self) -> tuple[object, ...]:
        return (
            self.source,
            _relation_sort_key(self.relation),
            self.target,
            self.source_role,
            self.target_role,
            _properties_sort_key(self.context),
            self.weight or "",
            self.provenance,
        )


@dataclass(frozen=True, slots=True)
class IdeonHyperedge:
    """An n-ary relation, optionally reified through a relation node."""

    relation: Relation
    roles: tuple[RoleBinding, ...]
    relation_node: str | None = None
    context: tuple[Property, ...] = ()
    weight: str | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_relation(self.relation)
        roles = tuple(self.roles)
        if not roles:
            raise ValueError("a hyperedge must contain at least one role binding")
        if any(not isinstance(role, RoleBinding) for role in roles):
            raise TypeError("hyperedge roles must contain RoleBinding values")
        if len(set(roles)) != len(roles):
            raise ValueError("hyperedge roles must not contain duplicate bindings")
        object.__setattr__(self, "roles", roles)
        if self.relation_node is not None:
            _require_text(self.relation_node, "relation node id")
        if self.weight is not None:
            _require_text(self.weight, "hyperedge weight")
        object.__setattr__(
            self, "context", _normalise_properties(self.context, "hyperedge context")
        )
        object.__setattr__(
            self,
            "provenance",
            _normalise_strings(self.provenance, "hyperedge provenance"),
        )

    def sort_key(self) -> tuple[object, ...]:
        return (
            _relation_sort_key(self.relation),
            self.roles,
            self.relation_node or "",
            _properties_sort_key(self.context),
            self.weight or "",
            self.provenance,
        )


@dataclass(frozen=True, slots=True)
class IdeonGraph:
    """A contained, possibly disconnected, authoritative idea graph."""

    identity: IdeonIdentity
    nodes: tuple[IdeonNode, ...] = ()
    edges: tuple[IdeonEdge, ...] = ()
    hyperedges: tuple[IdeonHyperedge, ...] = ()
    contracts: tuple[str, ...] = ()
    openings: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    annotations: tuple[Property, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.identity, IdeonIdentity):
            raise TypeError("identity must be an IdeonIdentity")

        nodes = tuple(self.nodes)
        if any(not isinstance(node, IdeonNode) for node in nodes):
            raise TypeError("nodes must contain IdeonNode values")
        node_ids = tuple(node.node_id for node in nodes)
        if len(set(node_ids)) != len(node_ids):
            raise ValueError("node ids must be unique within an Ideon graph")
        nodes = tuple(sorted(nodes, key=lambda node: node.node_id))
        object.__setattr__(self, "nodes", nodes)
        node_by_id = {node.node_id: node for node in nodes}

        for root in self.identity.roots:
            if root not in node_by_id:
                raise ValueError(f"identity root references unknown node: {root}")

        edges = tuple(self.edges)
        if any(not isinstance(edge, IdeonEdge) for edge in edges):
            raise TypeError("edges must contain IdeonEdge values")
        for edge in edges:
            if edge.source not in node_by_id:
                raise ValueError(f"edge source references unknown node: {edge.source}")
            if edge.target not in node_by_id:
                raise ValueError(f"edge target references unknown node: {edge.target}")
        if len(set(edges)) != len(edges):
            raise ValueError("edges must not contain duplicates")
        object.__setattr__(self, "edges", tuple(sorted(edges, key=IdeonEdge.sort_key)))

        hyperedges = tuple(self.hyperedges)
        if any(not isinstance(edge, IdeonHyperedge) for edge in hyperedges):
            raise TypeError("hyperedges must contain IdeonHyperedge values")
        for hyperedge in hyperedges:
            for role in hyperedge.roles:
                if role.node_id not in node_by_id:
                    raise ValueError(f"hyperedge role references unknown node: {role.node_id}")
            if hyperedge.relation_node is not None:
                relation_node = node_by_id.get(hyperedge.relation_node)
                if relation_node is None:
                    raise ValueError(
                        "hyperedge relation_node references unknown node: "
                        f"{hyperedge.relation_node}"
                    )
                if relation_node.kind is not NodeKind.RELATION:
                    raise ValueError("hyperedge relation_node must have RELATION kind")
        if len(set(hyperedges)) != len(hyperedges):
            raise ValueError("hyperedges must not contain duplicates")
        object.__setattr__(
            self,
            "hyperedges",
            tuple(sorted(hyperedges, key=IdeonHyperedge.sort_key)),
        )

        object.__setattr__(self, "contracts", _normalise_strings(self.contracts, "contracts"))
        object.__setattr__(self, "openings", _normalise_strings(self.openings, "openings"))
        object.__setattr__(
            self, "provenance", _normalise_strings(self.provenance, "graph provenance")
        )
        object.__setattr__(
            self, "annotations", _normalise_properties(self.annotations, "annotations")
        )
