"""Bounded executable catalogue contracts and independent acceptance checks."""

from __future__ import annotations

import json
import random
from dataclasses import replace

import pytest

from af.domain.algorithms import (
    AlgorithmError,
    AlgorithmRequirements,
    ReferenceAlgorithms,
    parse_input,
    verify_output,
)


def test_sort_search_string_graph_and_reduction_have_independent_acceptance():
    catalogue = ReferenceAlgorithms()
    vectors = (
        ("insertion-sort", {"values": [3, -1, 3, 0]}, [-1, 0, 3, 3]),
        ("merge-sort", {"values": [3, -1, 3, 0]}, [-1, 0, 3, 3]),
        ("binary-first", {"values": [-1, 0, 3, 3], "target": 3}, 2),
        ("linear-membership", {"values": [3, -1, 0], "target": 9}, False),
        ("sum-integers", {"values": [3, -1, 3, 0]}, 5),
        ("string-matches", {"text": "ééé", "pattern": "éé"}, [0, 1]),
        (
            "breadth-first",
            {"nodes": 5, "edges": [[0, 2], [0, 1], [1, 3]], "start": 0},
            [0, 1, 2, 3],
        ),
        (
            "shortest-path",
            {"nodes": 5, "edges": [[0, 2], [0, 1], [1, 3]], "start": 0, "target": 3},
            [0, 1, 3],
        ),
        ("sorted-membership", {"values": [3, -1, 3, 0], "target": 3}, True),
    )
    for name, data, expected in vectors:
        result = catalogue.execute(name, json.dumps(data).encode())
        assert result.value == expected
        assert result.verified
        assert result.catalogue_digest == catalogue.digest
        assert result.input_digest.startswith("sha256:")
        assert verify_output(parse_input(name, json.dumps(data).encode()), result.value)
        assert not verify_output(parse_input(name, json.dumps(data).encode()), {"forged": True})


def test_property_vectors_preserve_sort_multiset_first_match_and_sum():
    rng = random.Random(821)
    catalogue = ReferenceAlgorithms()
    for n in range(65):
        values = [rng.randrange(-20, 21) for _ in range(n)]
        for name in ("insertion-sort", "merge-sort"):
            assert catalogue.execute(name, json.dumps({"values": values}).encode()).value == sorted(
                values
            )
        sorted_values = sorted(values)
        for target in (-99, -1, 0, 9, 99):
            payload = json.dumps({"values": sorted_values, "target": target}).encode()
            result = catalogue.execute("binary-first", payload)
            assert result.value == (sorted_values.index(target) if target in values else None)
        assert catalogue.execute(
            "sum-integers", json.dumps({"values": values}).encode()
        ).value == sum(values)


@pytest.mark.parametrize(
    "name,payload",
    [
        ("insertion-sort", b'{"values":[true]}'),
        ("insertion-sort", b'{"values":[1.0]}'),
        ("insertion-sort", b'{"values":[],"values":[2]}'),
        ("insertion-sort", b'{"values":[],"code":"raise Exception"}'),
        ("binary-first", b'{"values":[2,1],"target":1}'),
        ("binary-first", b'{"values":[1]}'),
        ("string-matches", b'{"text":"abc","pattern":""}'),
        ("breadth-first", b'{"nodes":2,"edges":[[0,2]],"start":0}'),
        ("breadth-first", b'{"nodes":2,"edges":[[0,1],[0,1]],"start":0}'),
        ("shortest-path", b'{"nodes":2,"edges":[],"start":0,"target":2}'),
        ("sum-integers", b'{"values":[9223372036854775808]}'),
        ("__import__", b"{}"),
        ("sum-integers", b"[]"),
        ("sum-integers", b"\xff"),
        ("sum-integers", b"{"),
    ],
)
def test_malformed_unsafe_or_incompatible_inputs_fail_closed(name, payload):
    with pytest.raises(AlgorithmError):
        ReferenceAlgorithms().execute(name, payload)


def test_size_bounds_and_unknown_properties_do_not_become_favourable():
    catalogue = ReferenceAlgorithms()
    with pytest.raises(AlgorithmError):
        catalogue.execute("merge-sort", json.dumps({"values": [1] * 1025}).encode())
    with pytest.raises(AlgorithmError):
        catalogue.execute("sum-integers", b" " * 32769)
    assert (
        catalogue.select(AlgorithmRequirements(family="ordering", max_items=10)).chosen
        == "insertion-sort"
    )
    assert (
        catalogue.select(AlgorithmRequirements(family="ordering", max_items=100)).chosen
        == "merge-sort"
    )
    assert (
        catalogue.select(AlgorithmRequirements(family="ordering", max_items=None)).status
        == "needs_information"
    )
    assert (
        catalogue.select(
            AlgorithmRequirements(family="ordering", max_items=100, max_memory_bytes=1)
        ).status
        == "needs_evidence"
    )
    assert (
        catalogue.select(AlgorithmRequirements(family="unsupported", max_items=1)).status
        == "no_route"
    )


def test_snapshot_replay_and_quarantine_preserve_old_catalogue():
    original = ReferenceAlgorithms()
    replay = ReferenceAlgorithms.from_snapshot(original.snapshot())
    assert replay.digest == original.digest
    assert replay.snapshot() == original.snapshot()
    quarantined = original.quarantine("insertion-sort", reason="oracle_disagreement")
    assert (
        original.select(AlgorithmRequirements(family="ordering", max_items=3)).chosen
        == "insertion-sort"
    )
    assert (
        quarantined.select(AlgorithmRequirements(family="ordering", max_items=3)).chosen
        == "merge-sort"
    )
    with pytest.raises(AlgorithmError, match="quarantined"):
        quarantined.execute("insertion-sort", b'{"values":[]}')
    with pytest.raises(AlgorithmError, match="snapshot"):
        replay.execute("merge-sort", b'{"values":[]}', expected_snapshot="sha256:" + "0" * 64)
    record = json.loads(original.snapshot())
    record["algorithms"][0]["provenance"]["code_license"] = "invented-public-domain"
    with pytest.raises(AlgorithmError):
        ReferenceAlgorithms.from_snapshot(json.dumps(record).encode())
    with pytest.raises(AlgorithmError):
        ReferenceAlgorithms.from_snapshot(b'{"import":"os"}')
    assert ReferenceAlgorithms.from_snapshot(quarantined.snapshot()).digest == quarantined.digest


def test_graph_acceptance_rejects_valid_but_nonshortest_path_and_missing_reachable_node():
    request = parse_input(
        "shortest-path", b'{"nodes":4,"edges":[[0,1],[1,2],[0,2],[2,3]],"start":0,"target":3}'
    )
    assert verify_output(request, [0, 2, 3])
    assert not verify_output(request, [0, 1, 2, 3])
    assert not verify_output(request, None)
    unreachable = parse_input("shortest-path", b'{"nodes":3,"edges":[[0,1]],"start":0,"target":2}')
    assert verify_output(unreachable, None)
    reachable = parse_input("breadth-first", b'{"nodes":3,"edges":[[0,1],[1,2]],"start":0}')
    assert not verify_output(reachable, [0, 1])
    assert not verify_output(reachable, [0, 2, 1])


def test_descriptor_identity_separates_method_implementation_and_structure():
    catalogue = ReferenceAlgorithms()
    descriptor = catalogue.descriptor("merge-sort")
    assert descriptor.algorithm_id != descriptor.implementation_id
    assert descriptor.provenance.code_license == "LicenseRef-AF-Repository"
    assert descriptor.provenance.concept_license is None
    assert descriptor.complexity.worst_time == "O(n log n)"
    assert descriptor.complexity.expected_time is None
    assert catalogue.structures[0].durable is False
    assert catalogue.structures[0].thread_safe is None
    with pytest.raises(AlgorithmError):
        catalogue.quarantine("merge-sort", reason="")
    with pytest.raises(AlgorithmError):
        catalogue.descriptor("unknown")
    with pytest.raises(AlgorithmError):
        replace(AlgorithmRequirements(family="ordering"), max_items=-1)


def test_machine_schemas_quarantine_dependencies_and_untrusted_snapshots():
    catalogue = ReferenceAlgorithms()
    snapshot = json.loads(catalogue.snapshot())
    assert snapshot["input_schemas"]["SearchInput"]["required"] == ["values", "target"]
    with pytest.raises(AlgorithmError, match="quarantined"):
        catalogue.quarantine("merge-sort", reason="failed_conformance").execute(
            "sorted-membership", b'{"values":[1],"target":1}'
        )
    snapshot["quarantined"] = [["merge-sort", "failed"], ["merge-sort", "failed"]]
    with pytest.raises(AlgorithmError):
        ReferenceAlgorithms.from_snapshot(json.dumps(snapshot).encode())
    for payload in (b'{"values":[NaN]}', b'{"values":[Infinity]}'):
        with pytest.raises(AlgorithmError):
            catalogue.execute("sum-integers", payload)


def test_selection_does_not_silently_assume_search_or_graph_preconditions():
    catalogue = ReferenceAlgorithms()
    assert (
        catalogue.select(AlgorithmRequirements(family="search", max_items=10)).status
        == "needs_information"
    )
    assert (
        catalogue.select(AlgorithmRequirements(family="path", max_items=10)).status
        == "needs_information"
    )
    with pytest.raises(AlgorithmError):
        AlgorithmRequirements(family="ordering", max_items=10, stable=1)
