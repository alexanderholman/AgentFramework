from __future__ import annotations

from dataclasses import replace

import pytest

from af.domain.ideons import (
    CanonicalKeyPolicy,
    IdentityRole,
    IdeonEdge,
    IdeonGraph,
    IdeonHyperedge,
    IdeonIdentity,
    IdeonKind,
    IdeonNode,
    IdeonNotFoundError,
    IdeonRef,
    NamespaceConflictError,
    NodeKind,
    Property,
    RoleBinding,
    StableKeyConflictError,
    canonical_record,
)
from af.domain.ideons.forms import LosslessFormEncoder
from af.domain.ideons.numeric import integer_to_decimal, natural_to_decimal, natural_to_hex
from af.domain.ideons.records import Decision, DecisionTrace, LanguageProfileRef
from af.domain.ideons.registry import is_prime_number
from af.infrastructure.ideons import InMemoryIdeonRegistry

REVISION = "a" * 64


def identity(
    name: str = "value",
    *,
    roots: tuple[str, ...] = (),
    kind: IdeonKind = IdeonKind.COMPOSITE,
) -> IdeonIdentity:
    return IdeonIdentity("test/core@1", "principia@1", name, kind=kind, roots=roots)


@pytest.mark.parametrize(
    "build",
    [
        lambda: IdeonRef(True, REVISION),
        lambda: IdeonRef(1, "A" * 64),
        lambda: Property("", "value"),
        lambda: Property("value", 1.5),  # type: ignore[arg-type]
        lambda: IdentityRole("", 1),
        lambda: IdentityRole("role", 0),
        lambda: IdeonIdentity("", "rules", "name"),
        lambda: IdeonIdentity("namespace", "rules", "name", kind="prime"),  # type: ignore[arg-type]
        lambda: IdeonIdentity("namespace", "rules", "name", roles=("bad",)),  # type: ignore[arg-type]
        lambda: IdeonIdentity(
            "namespace",
            "rules",
            "name",
            roles=(IdentityRole("role", 3), IdentityRole("role", 3)),
        ),
        lambda: IdeonIdentity("namespace", "rules", "name", order=(0,)),
        lambda: IdeonIdentity("namespace", "rules", "name", roots=("root", "root")),
        lambda: IdeonIdentity(
            "namespace",
            "rules",
            "name",
            context=(Property("same", 1), Property("same", 2)),
        ),
        lambda: IdeonNode("node", kind="entity"),  # type: ignore[arg-type]
        lambda: IdeonNode("node", ideon="bad"),  # type: ignore[arg-type]
        lambda: IdeonNode("node", type_ref="bad"),  # type: ignore[arg-type]
        lambda: IdeonNode("node", properties=("bad",)),  # type: ignore[arg-type]
        lambda: IdeonNode("node", provenance=("same", "same")),
        lambda: IdeonEdge("a", object(), "b"),  # type: ignore[arg-type]
        lambda: IdeonEdge("a", "relation", "b", weight=""),
        lambda: IdeonHyperedge("relation", ()),
        lambda: IdeonHyperedge("relation", ("bad",)),  # type: ignore[arg-type]
        lambda: IdeonHyperedge(
            "relation",
            (RoleBinding("role", "a"), RoleBinding("role", "a")),
        ),
        lambda: IdeonHyperedge("relation", (RoleBinding("role", "a"),), relation_node=""),
        lambda: IdeonGraph("bad"),  # type: ignore[arg-type]
        lambda: IdeonGraph(identity(), nodes=("bad",)),  # type: ignore[arg-type]
        lambda: IdeonGraph(identity(), nodes=(IdeonNode("same"), IdeonNode("same"))),
        lambda: IdeonGraph(identity(roots=("missing",))),
        lambda: DecisionTrace(Decision.ASK, ("rule",), policy=(("b", "2"), ("a", "1"))),
        lambda: DecisionTrace(Decision.ASK, ("rule",), policy=(("same", "1"), ("same", "2"))),
    ],
)
def test_invalid_graph_values_fail_at_construction(build) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError)):
        build()


@pytest.mark.parametrize(
    "build",
    [
        lambda: IdeonGraph(
            identity(),
            nodes=(IdeonNode("a"),),
            edges=(IdeonEdge("missing", "relation", "a"),),
        ),
        lambda: IdeonGraph(
            identity(),
            nodes=(IdeonNode("a"),),
            edges=(IdeonEdge("a", "relation", "missing"),),
        ),
        lambda: IdeonGraph(
            identity(),
            nodes=(IdeonNode("a"), IdeonNode("b")),
            edges=(
                IdeonEdge("a", "relation", "b"),
                IdeonEdge("a", "relation", "b"),
            ),
        ),
        lambda: IdeonGraph(identity(), hyperedges=("bad",)),  # type: ignore[arg-type]
        lambda: IdeonGraph(
            identity(),
            nodes=(IdeonNode("a"),),
            hyperedges=(IdeonHyperedge("relation", (RoleBinding("role", "missing"),)),),
        ),
        lambda: IdeonGraph(
            identity(),
            nodes=(IdeonNode("a"),),
            hyperedges=(
                IdeonHyperedge(
                    "relation",
                    (RoleBinding("role", "a"),),
                    relation_node="missing",
                ),
            ),
        ),
        lambda: IdeonGraph(
            identity(),
            nodes=(IdeonNode("a"),),
            hyperedges=(
                IdeonHyperedge("relation", (RoleBinding("role", "a"),), relation_node="a"),
            ),
        ),
        lambda: IdeonGraph(identity(), contracts=("same", "same")),
        lambda: IdeonGraph(identity(), annotations=(Property("same", 1), Property("same", 2))),
    ],
)
def test_invalid_graph_topology_fails_closed(build) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError)):
        build()


def test_all_property_and_relation_variants_have_canonical_records() -> None:
    ref = IdeonRef(3, REVISION)
    graph = IdeonGraph(
        identity("typed", roots=("a", "b", "relation")),
        nodes=(
            IdeonNode(
                "a",
                properties=(
                    Property("boolean", True),
                    Property("integer", 4),
                    Property("null", None),
                    Property("reference", ref),
                    Property("string", "value"),
                ),
            ),
            IdeonNode("b"),
            IdeonNode("relation", NodeKind.RELATION),
        ),
        edges=(IdeonEdge("a", ref, "b", context=(Property("qualified", True),)),),
        hyperedges=(
            IdeonHyperedge(
                ref,
                (RoleBinding("origin", "a"), RoleBinding("destination", "b")),
                relation_node="relation",
                weight="1/2",
            ),
        ),
    )
    record = canonical_record(graph)

    assert record["edges"][0]["relation"]["type"] == "ideon-ref"  # type: ignore[index]
    with pytest.raises(TypeError, match="IdeonIdentity or IdeonGraph"):
        canonical_record(object())  # type: ignore[arg-type]


def test_form_encoder_rejects_a_misdeclared_unicode_database_version() -> None:
    profile = LanguageProfileRef("en", "0.1", "999.0")

    with pytest.raises(ValueError, match="Unicode version does not match"):
        LosslessFormEncoder().encode("text", profile)


def test_language_profile_identity_includes_unicode_and_normalization_contracts() -> None:
    nfc = LanguageProfileRef("en", "0.1", "15.0.0", normalization="NFC")
    nfkc = LanguageProfileRef("en", "0.1", "15.0.0", normalization="NFKC")
    other_unicode = LanguageProfileRef("en", "0.1", "15.1.0", normalization="NFC")

    assert len({nfc.identifier, nfkc.identifier, other_unicode.identifier}) == 3


def test_arbitrary_precision_number_codecs_do_not_use_the_decimal_digit_limit() -> None:
    huge = 10**5_000 + 123

    encoded = natural_to_decimal(huge)

    assert len(encoded) == 5_001
    assert encoded.endswith("123")
    assert integer_to_decimal(-huge) == "-" + encoded
    assert natural_to_decimal(0) == "0"
    assert natural_to_hex(255) == "ff"
    with pytest.raises(TypeError, match="integer"):
        integer_to_decimal(True)
    with pytest.raises(ValueError, match="natural number"):
        natural_to_decimal(-1)
    with pytest.raises(ValueError, match="natural number"):
        natural_to_hex(False)


def test_constructed_identity_binary_codec_covers_all_property_types() -> None:
    ref = IdeonRef(3, REVISION)
    typed = IdeonIdentity(
        "test/core@1",
        "principia@1",
        "typed-context",
        context=(
            Property("boolean", True),
            Property("integer", -7),
            Property("null", None),
            Property("reference", ref),
            Property("string", "value"),
        ),
    )

    key = CanonicalKeyPolicy().key_for(typed)

    assert key > 0
    assert key % 2 == 0


def test_manifest_primality_is_exact_for_the_declared_64_bit_lane() -> None:
    assert is_prime_number(2)
    assert is_prime_number(2**61 - 1)
    assert not is_prime_number(341_550_071_728_321)
    assert not is_prime_number(49)
    assert not is_prime_number(1 << 64)


def test_registry_validates_namespace_types_keys_and_missing_lookups() -> None:
    with pytest.raises(ValueError, match="registry version"):
        InMemoryIdeonRegistry("")
    with pytest.raises(ValueError, match="registry namespace"):
        InMemoryIdeonRegistry("v1", namespace="")

    store = InMemoryIdeonRegistry("v1", namespace="test/core@1")
    graph = IdeonGraph(identity("known"))
    ref = store.register(graph)
    assert store.version == "v1"
    assert store.namespace == "test/core@1"
    assert store.key_for_identity(graph.identity) == ref.key

    with pytest.raises(NamespaceConflictError):
        store.intern(IdeonGraph(IdeonIdentity("other/core@1", "principia@1", "other")))
    with pytest.raises(TypeError, match="IdeonGraph"):
        store.intern("bad")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="IdeonGraph"):
        store.revise(ref.key, "bad")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exact IdeonRef"):
        store.resolve(ref.key)  # type: ignore[arg-type]
    with pytest.raises(IdeonNotFoundError):
        store.revisions(2)
    with pytest.raises(TypeError, match="IdeonIdentity"):
        store.key_for_identity("bad")  # type: ignore[arg-type]
    with pytest.raises(IdeonNotFoundError):
        store.key_for_identity(identity("missing"))

    dangling = IdeonRef(999, "b" * 64)
    with pytest.raises(IdeonNotFoundError, match="unresolved exact Ideon reference"):
        store.intern(IdeonGraph(identity("dangling"), nodes=(IdeonNode("x", ideon=dangling),)))


def test_registry_requires_manifested_prime_keys_and_detects_key_policy_conflicts() -> None:
    store = InMemoryIdeonRegistry("v1")
    explicit = store.intern(IdeonGraph(identity("explicit", kind=IdeonKind.PRIME)), stable_key=3)
    assert explicit.key == 3
    with pytest.raises(ValueError, match="explicit manifested stable key"):
        store.intern(IdeonGraph(identity("automatic", kind=IdeonKind.PRIME)))
    assert not is_prime_number(False)
    assert not is_prime_number(25)

    class ConstantPolicy:
        def key_for(self, _: IdeonIdentity) -> int:
            return 100

    collisions = InMemoryIdeonRegistry("v1", key_policy=ConstantPolicy())
    collisions.intern(IdeonGraph(identity("first")))
    with pytest.raises(StableKeyConflictError):
        collisions.intern(IdeonGraph(identity("second")))

    class OddPolicy:
        def key_for(self, _: IdeonIdentity) -> int:
            return 101

    with pytest.raises(ValueError, match="must be even"):
        InMemoryIdeonRegistry("v1", key_policy=OddPolicy()).intern(IdeonGraph(identity("odd")))

    assert CanonicalKeyPolicy().key_for(identity("constructed")) % 2 == 0


def test_registry_rejects_explicit_key_change_for_an_existing_graph() -> None:
    store = InMemoryIdeonRegistry("v1")
    graph = IdeonGraph(identity("same"))
    ref = store.intern(graph)
    with pytest.raises(StableKeyConflictError):
        store.intern(graph, stable_key=ref.key + 2)
    with pytest.raises(IdeonNotFoundError):
        store.revise(ref.key + 2, replace(graph, provenance=("new",)))
