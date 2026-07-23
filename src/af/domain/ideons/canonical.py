"""Canonical records, immutable revisions, and stable Ideon key construction."""

from __future__ import annotations

import hashlib
import json

from .models import (
    IdeonEdge,
    IdeonGraph,
    IdeonHyperedge,
    IdeonIdentity,
    IdeonKind,
    IdeonNode,
    IdeonRef,
    Property,
    PropertyValue,
    Relation,
    RoleBinding,
)
from .numeric import integer_to_decimal, natural_to_hex

type JsonScalar = None | bool | int | str
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type CanonicalIdeon = IdeonIdentity | IdeonGraph


_KEY_DOMAIN = b"AF-IDEON-KEY-v1\x00"
_BINARY_IDENTITY_DOMAIN = b"AF-IDEON-IDENTITY-BINARY-v1\x00"


def _length_prefix(payload: bytes) -> bytes:
    if len(payload) >= 1 << 64:
        raise ValueError("canonical identity field exceeds the 64-bit length limit")
    return len(payload).to_bytes(8, "big") + payload


def _binary_text(value: str) -> bytes:
    return _length_prefix(value.encode("utf-8"))


def _binary_natural(value: int) -> bytes:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("canonical natural must be a non-negative integer")
    width = max(1, (value.bit_length() + 7) // 8)
    return _length_prefix(value.to_bytes(width, "big"))


def _binary_integer(value: int) -> bytes:
    sign = b"-" if value < 0 else b"+"
    return sign + _binary_natural(abs(value))


def _binary_sequence(values: tuple[bytes, ...]) -> bytes:
    return _binary_natural(len(values)) + b"".join(_length_prefix(value) for value in values)


def _binary_property_value(value: PropertyValue) -> bytes:
    if isinstance(value, IdeonRef):
        return b"r" + _binary_natural(value.key) + _binary_text(value.revision)
    if value is None:
        return b"n"
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, int):
        return b"i" + _binary_integer(value)
    return b"s" + _binary_text(value)


def _binary_property(value: Property) -> bytes:
    return _binary_text(value.name) + _length_prefix(_binary_property_value(value.value))


def binary_identity_bytes(identity: IdeonIdentity) -> bytes:
    """Encode stable identity injectively without decimal re-encoding of child keys."""

    roles = tuple(_binary_text(role.role) + _binary_natural(role.key) for role in identity.roles)
    order = tuple(_binary_natural(key) for key in identity.order)
    context = tuple(_binary_property(value) for value in identity.context)
    roots = tuple(_binary_text(root) for root in identity.roots)
    return b"".join(
        (
            _BINARY_IDENTITY_DOMAIN,
            _binary_text(identity.namespace),
            _binary_text(identity.ruleset),
            _binary_text(identity.name),
            _binary_text(identity.kind.value),
            _binary_sequence(roles),
            _binary_sequence(order),
            _binary_sequence(context),
            _binary_sequence(roots),
        )
    )


def _key_record(key: int) -> dict[str, JsonValue]:
    return {"radix": 16, "digits": natural_to_hex(key)}


def _ref_record(ref: IdeonRef) -> dict[str, JsonValue]:
    return {"key": _key_record(ref.key), "revision": ref.revision}


def _property_value_record(value: PropertyValue) -> dict[str, JsonValue]:
    if isinstance(value, IdeonRef):
        return {"type": "ideon-ref", "value": _ref_record(value)}
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "bool", "value": value}
    if isinstance(value, int):
        return {"type": "int", "value": integer_to_decimal(value)}
    return {"type": "str", "value": value}


def _property_record(value: Property) -> dict[str, JsonValue]:
    return {"name": value.name, "value": _property_value_record(value.value)}


def _properties_record(values: tuple[Property, ...]) -> list[JsonValue]:
    return [_property_record(value) for value in values]


def _relation_record(value: Relation) -> dict[str, JsonValue]:
    if isinstance(value, IdeonRef):
        return {"type": "ideon-ref", "value": _ref_record(value)}
    return {"type": "label", "value": value}


def identity_record(identity: IdeonIdentity) -> dict[str, JsonValue]:
    """Return the canonical identity-bearing record.

    List order is retained for roles, composition order, and roots. Context has
    already been normalised as a mapping by :class:`IdeonIdentity`.
    """

    return {
        "schema": "af.ideon.identity/v1",
        "namespace": identity.namespace,
        "ruleset": identity.ruleset,
        "name": identity.name,
        "kind": identity.kind.value,
        "roles": [{"role": role.role, "key": _key_record(role.key)} for role in identity.roles],
        "order": [_key_record(key) for key in identity.order],
        "context": _properties_record(identity.context),
        "roots": list(identity.roots),
    }


def _node_record(node: IdeonNode) -> dict[str, JsonValue]:
    return {
        "id": node.node_id,
        "kind": node.kind.value,
        "ideon": None if node.ideon is None else _ref_record(node.ideon),
        "type_ref": None if node.type_ref is None else _ref_record(node.type_ref),
        "properties": _properties_record(node.properties),
        "provenance": list(node.provenance),
    }


def _edge_record(edge: IdeonEdge) -> dict[str, JsonValue]:
    return {
        "source": edge.source,
        "relation": _relation_record(edge.relation),
        "target": edge.target,
        "source_role": edge.source_role,
        "target_role": edge.target_role,
        "context": _properties_record(edge.context),
        "weight": edge.weight,
        "provenance": list(edge.provenance),
    }


def _role_record(role: RoleBinding) -> dict[str, JsonValue]:
    return {"role": role.role, "node_id": role.node_id}


def _hyperedge_record(edge: IdeonHyperedge) -> dict[str, JsonValue]:
    return {
        "relation": _relation_record(edge.relation),
        "roles": [_role_record(role) for role in edge.roles],
        "relation_node": edge.relation_node,
        "context": _properties_record(edge.context),
        "weight": edge.weight,
        "provenance": list(edge.provenance),
    }


def graph_record(graph: IdeonGraph) -> dict[str, JsonValue]:
    """Return the complete canonical, key-free authoritative graph record."""

    return {
        "schema": "af.ideon.graph/v1",
        "identity": identity_record(graph.identity),
        "nodes": [_node_record(node) for node in graph.nodes],
        "edges": [_edge_record(edge) for edge in graph.edges],
        "hyperedges": [_hyperedge_record(edge) for edge in graph.hyperedges],
        "contracts": list(graph.contracts),
        "openings": list(graph.openings),
        "provenance": list(graph.provenance),
        "annotations": _properties_record(graph.annotations),
    }


def canonical_record(value: CanonicalIdeon) -> dict[str, JsonValue]:
    """Build the versioned canonical record for an identity or graph."""

    if isinstance(value, IdeonGraph):
        return graph_record(value)
    if isinstance(value, IdeonIdentity):
        return identity_record(value)
    raise TypeError("canonical records support IdeonIdentity or IdeonGraph values")


def canonical_bytes(value: CanonicalIdeon) -> bytes:
    """Encode canonical JSON as byte-identical UTF-8 without insignificant space."""

    return json.dumps(
        canonical_record(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def identity_signature(identity: IdeonIdentity) -> str:
    """Return the deterministic SHA-256 diagnostic signature of an identity."""

    return hashlib.sha256(canonical_bytes(identity)).hexdigest()


def revision_id(graph: IdeonGraph) -> str:
    """Return the immutable SHA-256 revision for the full canonical graph."""

    return hashlib.sha256(canonical_bytes(graph)).hexdigest()


def composite_key_for(identity: IdeonIdentity) -> int:
    """Construct a deterministic, positive, collision-free even key.

    A versioned, length-prefixed binary identity is read as one unsigned
    big-endian integer, then shifted left by one bit. Natural child keys are
    encoded directly as minimal bytes rather than recursively as decimal JSON.
    The mapping is injective over identities while keeping nested growth linear
    in the encoded child-key data. The left shift reserves odd primes for prime
    Ideons; Python's arbitrary-precision integers preserve the complete value.
    """

    if identity.kind is IdeonKind.PRIME:
        raise ValueError("prime Ideon keys are allocated and manifested by a registry")
    raw_key = int.from_bytes(_KEY_DOMAIN + binary_identity_bytes(identity), "big", signed=False)
    return raw_key << 1


def stable_key_for(identity: IdeonIdentity) -> int:
    """Backward-compatible public name for constructed non-prime keys."""

    return composite_key_for(identity)
