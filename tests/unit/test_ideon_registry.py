from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from af.domain.ideons import (
    IdentityConflictError,
    IdentityRole,
    IdeonEdge,
    IdeonGraph,
    IdeonHyperedge,
    IdeonIdentity,
    IdeonKind,
    IdeonNode,
    IdeonNotFoundError,
    IdeonRef,
    NodeKind,
    Property,
    RoleBinding,
    StableKeyConflictError,
    canonical_bytes,
    identity_signature,
    is_prime_number,
    revision_id,
    stable_key_for,
)
from af.infrastructure.ideons import InMemoryIdeonRegistry


def identity(
    name: str = "example",
    *,
    kind: IdeonKind = IdeonKind.COMPOSITE,
    roles: tuple[IdentityRole, ...] = (),
    order: tuple[int, ...] = (),
    context: tuple[Property, ...] = (),
    roots: tuple[str, ...] = (),
) -> IdeonIdentity:
    return IdeonIdentity(
        namespace="test/core@1",
        ruleset="principia@1",
        name=name,
        kind=kind,
        roles=roles,
        order=order,
        context=context,
        roots=roots,
    )


class IdeonCanonicalisationTests(unittest.TestCase):
    def test_revision_is_sha256_of_byte_identical_canonical_json(self) -> None:
        first = IdeonGraph(
            identity=identity(
                roots=("root",),
                context=(Property("domain", "test"), Property("literal", True)),
            ),
            nodes=(
                IdeonNode("other", NodeKind.VALUE),
                IdeonNode("root", NodeKind.ENTITY),
            ),
            annotations=(Property("z", 3), Property("a", None)),
            provenance=("source-b", "source-a"),
        )
        second = IdeonGraph(
            identity=identity(
                roots=("root",),
                context=(Property("literal", True), Property("domain", "test")),
            ),
            nodes=(
                IdeonNode("root", NodeKind.ENTITY),
                IdeonNode("other", NodeKind.VALUE),
            ),
            annotations=(Property("a", None), Property("z", 3)),
            provenance=("source-a", "source-b"),
        )

        encoded = canonical_bytes(first)
        self.assertEqual(encoded, canonical_bytes(second))
        self.assertEqual(revision_id(first), revision_id(second))
        self.assertRegex(revision_id(first), r"^[0-9a-f]{64}$")
        record = json.loads(encoded)
        self.assertEqual(record["identity"]["namespace"], "test/core@1")
        self.assertEqual(record["identity"]["ruleset"], "principia@1")
        self.assertEqual(record["identity"]["context"][0]["name"], "domain")

    def test_composition_is_order_and_role_sensitive(self) -> None:
        a = 11
        b = 13
        ab = identity(
            "ordered-composition",
            roles=(IdentityRole("left", a), IdentityRole("right", b)),
            order=(a, b),
        )
        ba = identity(
            "ordered-composition",
            roles=(IdentityRole("left", b), IdentityRole("right", a)),
            order=(b, a),
        )
        same_order_different_roles = identity(
            "ordered-composition",
            roles=(IdentityRole("origin", a), IdentityRole("destination", b)),
            order=(a, b),
        )

        self.assertNotEqual(identity_signature(ab), identity_signature(ba))
        self.assertNotEqual(stable_key_for(ab), stable_key_for(ba))
        self.assertNotEqual(stable_key_for(ab), stable_key_for(same_order_different_roles))
        self.assertEqual(stable_key_for(ab) % 2, 0)
        self.assertEqual(stable_key_for(ba) % 2, 0)

    def test_deeply_nested_composite_keys_remain_serializable_and_resolvable(self) -> None:
        store = InMemoryIdeonRegistry(version="nested-test@1")
        current = store.intern(IdeonGraph(identity("nested-0")))
        for depth in range(1, 11):
            graph = IdeonGraph(
                identity(
                    f"nested-{depth}",
                    roles=(IdentityRole("child", current.key),),
                    order=(current.key,),
                ),
                nodes=(IdeonNode("child", ideon=current),),
            )
            current = store.intern(graph)
            self.assertEqual(store.resolve(current), graph)
            self.assertEqual(canonical_bytes(graph), canonical_bytes(graph))

    def test_namespace_ruleset_and_identity_context_affect_the_key(self) -> None:
        base = identity(context=(Property("place", "river"),))
        other_context = identity(context=(Property("place", "finance"),))
        other_namespace = IdeonIdentity(
            namespace="test/expert@1",
            ruleset=base.ruleset,
            name=base.name,
            kind=base.kind,
            context=base.context,
        )
        other_ruleset = IdeonIdentity(
            namespace=base.namespace,
            ruleset="principia@2",
            name=base.name,
            kind=base.kind,
            context=base.context,
        )

        keys = {
            stable_key_for(base),
            stable_key_for(other_context),
            stable_key_for(other_namespace),
            stable_key_for(other_ruleset),
        }
        self.assertEqual(len(keys), 4)
        self.assertTrue(all(key > 0 for key in keys))

    def test_graph_and_reference_value_objects_are_immutable(self) -> None:
        graph = IdeonGraph(identity=identity())
        with self.assertRaises(FrozenInstanceError):
            graph.identity = identity("replacement")  # type: ignore[misc]

        ref = IdeonRef(1, "a" * 64)
        with self.assertRaises(FrozenInstanceError):
            ref.key = 2  # type: ignore[misc]


class IdeonRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryIdeonRegistry(version="test-registry@1")

    def test_canonical_duplicate_is_interned_once(self) -> None:
        graph = IdeonGraph(
            identity=identity("deduplicated"),
            nodes=(IdeonNode("b"), IdeonNode("a")),
        )
        reordered = IdeonGraph(
            identity=identity("deduplicated"),
            nodes=(IdeonNode("a"), IdeonNode("b")),
        )

        first = self.store.intern(graph)
        second = self.store.intern(reordered)

        self.assertEqual(first, second)
        self.assertEqual(len(self.store), 1)
        self.assertIs(self.store.resolve(first), graph)

    def test_compatible_refinement_gets_new_revision_under_same_key(self) -> None:
        concept = identity("refined", kind=IdeonKind.PRIME, roots=("root",))
        original = IdeonGraph(
            identity=concept,
            nodes=(IdeonNode("root", NodeKind.SENSE),),
        )
        refined = IdeonGraph(
            identity=concept,
            nodes=(
                IdeonNode("root", NodeKind.SENSE),
                IdeonNode("evidence", NodeKind.VALUE),
            ),
            provenance=("dictionary:second-edition",),
        )

        old_ref = self.store.intern(original, stable_key=3)
        new_ref = self.store.revise(old_ref.key, refined)

        self.assertEqual(old_ref.key, new_ref.key)
        self.assertNotEqual(old_ref.revision, new_ref.revision)
        self.assertEqual(self.store.revisions(old_ref.key), (old_ref, new_ref))
        self.assertIs(self.store.resolve(old_ref), original)
        self.assertIs(self.store.resolve(new_ref), refined)

    def test_revision_rejects_a_changed_identity(self) -> None:
        ref = self.store.intern(IdeonGraph(identity=identity("first")))

        with self.assertRaises(IdentityConflictError):
            self.store.revise(ref.key, IdeonGraph(identity=identity("other")))

    def test_explicit_key_conflicts_are_never_silently_reused(self) -> None:
        first = IdeonGraph(identity=identity("first"))
        second = IdeonGraph(identity=identity("second"))
        ref = self.store.intern(first, stable_key=100)

        self.assertEqual(ref.key, 100)
        with self.assertRaises(StableKeyConflictError):
            self.store.intern(second, stable_key=100)
        with self.assertRaises(StableKeyConflictError):
            self.store.intern(first, stable_key=102)

    def test_prime_ideons_receive_odd_mathematical_prime_keys(self) -> None:
        first = self.store.intern(
            IdeonGraph(identity=identity("prime-a", kind=IdeonKind.PRIME)), stable_key=3
        )
        second = self.store.intern(
            IdeonGraph(identity=identity("prime-b", kind=IdeonKind.PRIME)), stable_key=5
        )
        composite = self.store.intern(IdeonGraph(identity=identity("composite")))

        self.assertEqual((first.key, second.key), (3, 5))
        self.assertTrue(is_prime_number(first.key))
        self.assertTrue(is_prime_number(second.key))
        self.assertEqual(composite.key % 2, 0)
        self.assertEqual(
            self.store.manifest()[0],
            (
                first.key,
                identity_signature(identity("prime-a", kind=IdeonKind.PRIME)),
                IdeonKind.PRIME,
            ),
        )

    def test_explicit_prime_key_must_be_an_odd_mathematical_prime(self) -> None:
        prime = IdeonGraph(identity=identity("prime", kind=IdeonKind.PRIME))
        with self.assertRaises(ValueError, msg="nine is composite"):
            self.store.intern(prime, stable_key=9)
        with self.assertRaises(ValueError, msg="two belongs to the even key lane"):
            self.store.intern(prime, stable_key=2)
        with self.assertRaisesRegex(ValueError, "64-bit manifest lane"):
            self.store.intern(prime, stable_key=(1 << 64) + 13)

        ref = self.store.intern(prime, stable_key=13)
        self.assertEqual(ref.key, 13)

    def test_explicit_non_prime_key_must_use_the_even_lane(self) -> None:
        composite = IdeonGraph(identity=identity("composite"))
        with self.assertRaises(ValueError, msg="odd keys are reserved for prime Ideons"):
            self.store.intern(composite, stable_key=101)

        ref = self.store.intern(composite, stable_key=102)
        self.assertEqual(ref.key, 102)

        with self.assertRaises(ValueError, msg="prime keys require registry allocation"):
            stable_key_for(identity("prime", kind=IdeonKind.PRIME))

    def test_zero_is_rejected_for_keys_and_refs(self) -> None:
        with self.assertRaises(ValueError):
            IdeonRef(0, "a" * 64)
        with self.assertRaises(ValueError):
            self.store.intern(IdeonGraph(identity=identity()), stable_key=0)

    def test_resolve_requires_the_exact_revision_reference(self) -> None:
        ref = self.store.intern(IdeonGraph(identity=identity("known")))
        missing = IdeonRef(ref.key, "f" * 64)

        with self.assertRaises(IdeonNotFoundError):
            self.store.resolve(missing)

    def test_disconnected_semantic_islands_are_valid(self) -> None:
        graph = IdeonGraph(
            identity=identity("two-islands", roots=("island-a", "island-b")),
            nodes=(IdeonNode("island-a"), IdeonNode("island-b")),
        )

        ref = self.store.intern(graph)
        self.assertEqual(len(self.store.resolve(ref).nodes), 2)

    def test_compact_edges_and_relation_node_hyperedges_are_representable(self) -> None:
        graph = IdeonGraph(
            identity=identity("relations", roots=("alice",)),
            nodes=(
                IdeonNode("alice", NodeKind.ENTITY),
                IdeonNode("book", NodeKind.ENTITY),
                IdeonNode(
                    "giving",
                    NodeKind.RELATION,
                    provenance=("dictionary:gives",),
                ),
            ),
            edges=(IdeonEdge("alice", "holds", "book"),),
            hyperedges=(
                IdeonHyperedge(
                    relation="gives",
                    relation_node="giving",
                    roles=(
                        RoleBinding("giver", "alice"),
                        RoleBinding("theme", "book"),
                    ),
                    provenance=("sentence:0-16",),
                ),
            ),
        )

        resolved = self.store.resolve(self.store.intern(graph))
        self.assertEqual(resolved.edges[0].relation, "holds")
        self.assertEqual(resolved.hyperedges[0].relation_node, "giving")
        self.assertEqual(resolved.hyperedges[0].roles[0].role, "giver")

    def test_registered_prime_can_be_reused_in_multiple_composites(self) -> None:
        prime = self.store.intern(
            IdeonGraph(identity=identity("bank-river", kind=IdeonKind.PRIME)),
            stable_key=3,
        )
        first = IdeonGraph(
            identity=identity("first-use"),
            nodes=(IdeonNode("bank", NodeKind.SENSE, ideon=prime),),
        )
        second = IdeonGraph(
            identity=identity("second-use"),
            nodes=(IdeonNode("bank", NodeKind.SENSE, ideon=prime),),
        )

        first_ref = self.store.intern(first)
        second_ref = self.store.intern(second)

        self.assertNotEqual(first_ref.key, second_ref.key)
        self.assertEqual(self.store.resolve(first_ref).nodes[0].ideon, prime)
        self.assertEqual(self.store.resolve(second_ref).nodes[0].ideon, prime)


if __name__ == "__main__":
    unittest.main()
