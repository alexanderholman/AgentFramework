"""Problem identity, typed decomposition and whole-result acceptance."""

from __future__ import annotations

import json

import pytest

from af.domain.algorithms import AlgorithmError, ReferenceAlgorithms
from af.domain.algorithms.contracts import AlgorithmResult
from af.domain.problems import ProblemCatalogue, ProblemGraph, ProblemNode


def test_problem_identity_is_independent_of_algorithm_and_source_wording():
    problems = ProblemCatalogue(ReferenceAlgorithms())
    descriptor = problems.descriptor("integer-membership")
    assert descriptor.prime is True
    assert descriptor.frame.ontology == "af.closed-computational-problems@1.0.0"
    assert descriptor.id != "linear-membership"
    assert descriptor.frame.evidence_snapshot.startswith("sha256:")
    assert problems.match("contains", b'{"values":[3,2,3],"target":3}').problem_id == descriptor.id
    assert (
        problems.match("integer-membership", b'{"values":[3,2,3],"target":3}').match_type == "exact"
    )
    assert (
        problems.match("contains", b'{"values":[3,2,3],"target":3}').match_type == "declared_alias"
    )
    assert problems.match("probably contains", b"{}").status == "no_match"
    assert problems.match("contains", b'{"values":[]}').status == "under_specified"
    assert problems.match("contains", b'{"values":[true],"target":1}').status == "invalid"


def test_composite_preserves_original_acceptance_beyond_local_node_success():
    problems = ProblemCatalogue(ReferenceAlgorithms())
    graph = problems.decompose("order-and-locate")
    assert graph.nodes[1].depends_on == ("order",)
    assert graph.digest == problems.decompose("order-and-locate").digest
    result = problems.resolve("order-and-locate", b'{"values":[5,-1,5,3],"target":5}')
    assert result["value"] == {"ordered": [-1, 3, 5, 5], "index": 2}
    assert result["verified"] is True
    assert len(result["nodes"]) == 2
    assert result["problem_digest"] == problems.descriptor("order-and-locate").digest
    assert (
        problems.verify(
            "order-and-locate",
            b'{"values":[5,-1,5,3],"target":5}',
            {"ordered": [-1, 3, 5, 5], "index": 0},
        )
        is False
    )


@pytest.mark.parametrize(
    "nodes,output",
    [
        ((ProblemNode("a", "ordering", "b"), ProblemNode("b", "sorted-position", "a")), "b"),
        ((ProblemNode("a", "ordering"), ProblemNode("a", "ordering")), "a"),
        ((ProblemNode("a", "sorted-position", "missing"),), "a"),
        ((ProblemNode("a", "ordering"),), "missing"),
        ((ProblemNode("a", "integer-sum"), ProblemNode("b", "sorted-position", "a")), "b"),
        ((ProblemNode("a", "ordering"), ProblemNode("orphan", "ordering")), "a"),
    ],
)
def test_invalid_typed_decompositions_cannot_enter_routing(nodes, output):
    with pytest.raises(AlgorithmError):
        ProblemGraph(nodes, output)


def test_algorithm_port_is_replaceable_but_its_claims_cannot_verify_the_problem():
    class ForgedAlgorithms:
        digest = "sha256:" + "0" * 64

        def execute(self, name, data, *, expected_snapshot=None):
            return AlgorithmResult(name, "forged", self.digest, "forged", b"999", verified=True)

    problems = ProblemCatalogue(ForgedAlgorithms())
    with pytest.raises(AlgorithmError, match="acceptance"):
        problems.resolve("integer-sum", b'{"values":[1,2]}')


def test_all_prime_routes_and_unknown_inputs_fail_closed():
    problems = ProblemCatalogue(ReferenceAlgorithms())
    fixtures = (
        ("ordering", {"values": [2, 1]}, [1, 2]),
        ("sorted-position", {"values": [1, 2], "target": 3}, None),
        ("integer-membership", {"values": [], "target": 3}, False),
        ("integer-sum", {"values": []}, 0),
        ("substring-occurrences", {"text": "aaaa", "pattern": "aa"}, [0, 1, 2]),
        ("reachable-nodes", {"nodes": 2, "edges": [], "start": 0}, [0]),
        ("shortest-unweighted-path", {"nodes": 2, "edges": [], "start": 0, "target": 1}, None),
    )
    for name, data, expected in fixtures:
        assert problems.resolve(name, json.dumps(data).encode())["value"] == expected
    with pytest.raises(AlgorithmError):
        problems.resolve("unsupported", b"{}")
    assert not problems.verify("ordering", b'{"values":[1]}', [True])
    assert not problems.verify("order-and-locate", b'{"values":[1],"target":1}', [1])
    assert not problems.verify(
        "order-and-locate", b'{"values":[1],"target":1}', {"ordered": [1], "index": False}
    )


def test_problem_snapshot_replays_only_the_installed_ontology():
    original = ProblemCatalogue(ReferenceAlgorithms())
    replay = ProblemCatalogue.from_snapshot(original.snapshot(), ReferenceAlgorithms())
    assert replay.snapshot() == original.snapshot()
    tampered = json.loads(original.snapshot())
    tampered["descriptors"][0]["prime"] = False
    with pytest.raises(AlgorithmError, match="snapshot"):
        ProblemCatalogue.from_snapshot(json.dumps(tampered).encode(), ReferenceAlgorithms())
