"""Bounded, separately timed reference benchmark observations, never admission authority."""

from __future__ import annotations

import platform
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from time import perf_counter_ns
from typing import Literal

from af.domain.algorithms import AlgorithmError, ReferenceAlgorithms
from af.domain.algorithms.contracts import canonical, digest, parse_input
from af.domain.algorithms.implementations import evaluate, verify_output


@dataclass(frozen=True, slots=True)
class BenchmarkRecord:
    algorithm: str
    catalogue_digest: str
    descriptor_digest: str
    input_digest: str
    repetitions: int
    preflight_ns: int
    execution_ns: tuple[int, ...]
    verification_ns: tuple[int, ...]
    serialization_ns: tuple[int, ...]
    environment: tuple[tuple[str, str], ...]
    observed_at: datetime
    status: Literal["verified", "rejected"]
    reason: str

    def canonical_bytes(self) -> bytes:
        return canonical(
            {
                "schema": "af.algorithm-benchmark@1",
                "algorithm": self.algorithm,
                "catalogue_digest": self.catalogue_digest,
                "descriptor_digest": self.descriptor_digest,
                "input_digest": self.input_digest,
                "repetitions": self.repetitions,
                "preflight_ns": self.preflight_ns,
                "execution_ns": self.execution_ns,
                "verification_ns": self.verification_ns,
                "serialization_ns": self.serialization_ns,
                "environment": dict(self.environment),
                "environment_digest": digest(self.environment),
                "observed_at": self.observed_at.isoformat(),
                "status": self.status,
                "reason": self.reason,
                "memory_peak_bytes": None,
                "energy_joules": None,
                "host_load": None,
                "ranking_claim": None,
                "method": "one-preflight-then-warm-wall-clock-ns",
            }
        )


def benchmark_algorithm(
    catalogue: ReferenceAlgorithms, name: str, data: bytes, *, repetitions: int = 5
) -> BenchmarkRecord:
    if type(repetitions) is not int or not 1 <= repetitions <= 64:
        raise AlgorithmError("benchmark repetition bound is 1..64")
    start = perf_counter_ns()
    # Resolution checks snapshot, licensing-defined installed trust and quarantine, including
    # dependency quarantine. This preflight/warm-up is separately reported, not hidden.
    preflight = catalogue.execute(name, data)
    request = parse_input(name, data)
    preflight_ns = perf_counter_ns() - start
    execution: list[int] = []
    verification: list[int] = []
    serialization: list[int] = []
    passed = True
    for _ in range(repetitions):
        start = perf_counter_ns()
        value = evaluate(request)
        execution.append(perf_counter_ns() - start)
        start = perf_counter_ns()
        passed = verify_output(request, value)
        verification.append(perf_counter_ns() - start)
        start = perf_counter_ns()
        canonical(value)
        serialization.append(perf_counter_ns() - start)
        if not passed:
            break
    return BenchmarkRecord(
        name,
        catalogue.digest,
        digest(asdict(catalogue.descriptor(name))),
        preflight.input_digest,
        repetitions,
        preflight_ns,
        tuple(execution),
        tuple(verification),
        tuple(serialization),
        tuple(
            sorted(
                {
                    "python_implementation": platform.python_implementation(),
                    "python_version": platform.python_version(),
                    "machine": platform.machine(),
                    "system": platform.system(),
                    "release": platform.release(),
                }.items()
            )
        ),
        datetime.now(UTC),
        "verified" if passed else "rejected",
        "all_samples_verified" if passed else "oracle_disagreement",
    )
