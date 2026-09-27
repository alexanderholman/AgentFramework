"""Smoke benchmarks report actual bounded observations without inventing missing metrics."""

from __future__ import annotations

import json

import pytest

from af.application.algorithm_benchmarks import benchmark_algorithm
from af.domain.algorithms import AlgorithmError, ReferenceAlgorithms


def test_benchmark_records_environment_inputs_and_separate_verification_cost():
    result = benchmark_algorithm(
        ReferenceAlgorithms(), "merge-sort", b'{"values":[3,1,2]}', repetitions=3
    )
    record = json.loads(result.canonical_bytes())
    assert record["schema"] == "af.algorithm-benchmark@1"
    assert record["status"] == "verified"
    assert record["repetitions"] == 3
    assert record["catalogue_digest"].startswith("sha256:")
    assert record["input_digest"].startswith("sha256:")
    assert len(record["execution_ns"]) == len(record["verification_ns"]) == 3
    assert all(value >= 0 for value in record["execution_ns"] + record["verification_ns"])
    assert record["environment"]["python_version"]
    assert record["memory_peak_bytes"] is None
    assert record["energy_joules"] is None
    assert record["ranking_claim"] is None


def test_benchmark_preserves_failed_oracle_measurements_and_refuses_invalid_bounds(monkeypatch):
    import af.application.algorithm_benchmarks as module

    monkeypatch.setattr(module, "evaluate", lambda _: [99])
    result = benchmark_algorithm(
        ReferenceAlgorithms(), "merge-sort", b'{"values":[3,1]}', repetitions=2
    )
    payload = json.loads(result.canonical_bytes())
    assert payload["status"] == "rejected"
    assert payload["reason"] == "oracle_disagreement"
    assert len(payload["execution_ns"]) == 1
    for repetitions in (0, 65, True):
        with pytest.raises(AlgorithmError):
            benchmark_algorithm(
                ReferenceAlgorithms(), "merge-sort", b'{"values":[]}', repetitions=repetitions
            )
    with pytest.raises(AlgorithmError, match="quarantined"):
        benchmark_algorithm(
            ReferenceAlgorithms().quarantine("merge-sort", reason="failed"),
            "merge-sort",
            b'{"values":[]}',
            repetitions=1,
        )
