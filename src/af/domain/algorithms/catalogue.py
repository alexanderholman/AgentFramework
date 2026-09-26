"""Read-only offline reference catalogue with explicit quarantine and selection evidence."""

from __future__ import annotations

from dataclasses import asdict

from af.domain.algorithms.contracts import (
    MODELS,
    VERSION,
    AlgorithmDescriptor,
    AlgorithmError,
    AlgorithmRequirements,
    AlgorithmResult,
    Complexity,
    DataStructureDescriptor,
    SelectionPlan,
    canonical,
    digest,
    parse_input,
    parse_json,
)
from af.domain.algorithms.implementations import evaluate, verify_output


def _descriptor(
    name: str,
    family: str,
    schema: str,
    output: str,
    post: str,
    time: str,
    space: str,
    *,
    stable: bool | None = None,
    conditions: tuple[str, ...] = (),
    dependencies: tuple[str, ...] = (),
) -> AlgorithmDescriptor:
    return AlgorithmDescriptor(
        algorithm_id=f"af.algorithm.{name}@{VERSION}",
        family=family,
        implementation_id=f"af.python.reference.{name}@{VERSION}",
        input_schema=schema,
        output_schema=output,
        preconditions=conditions,
        postcondition=post,
        complexity=Complexity(time, space),
        stable=stable,
        dependencies=dependencies,
    )


DESCRIPTORS = (
    _descriptor(
        "insertion-sort",
        "ordering",
        "SequenceInput",
        "integer-sequence",
        "ascending permutation",
        "O(n²)",
        "O(n)",
        stable=True,
    ),
    _descriptor(
        "merge-sort",
        "ordering",
        "SequenceInput",
        "integer-sequence",
        "ascending permutation",
        "O(n log n)",
        "O(n)",
        stable=True,
    ),
    _descriptor(
        "binary-first",
        "search",
        "SearchInput",
        "index-or-null",
        "first equal index or null",
        "O(log n)",
        "O(1)",
        conditions=("ascending integer sequence",),
    ),
    _descriptor(
        "linear-membership",
        "membership",
        "SearchInput",
        "boolean",
        "target occurs iff true",
        "O(n)",
        "O(1)",
    ),
    _descriptor(
        "sum-integers",
        "aggregation",
        "SequenceInput",
        "integer",
        "exact mathematical sum",
        "O(n)",
        "O(1)",
        conditions=("bounded signed 64-bit inputs; arbitrary precision result",),
    ),
    _descriptor(
        "string-matches",
        "matching",
        "StringInput",
        "index-sequence",
        "all overlapping codepoint matches",
        "O(n*m)",
        "O(n)",
        conditions=("nonempty pattern; no Unicode normalization",),
    ),
    _descriptor(
        "breadth-first",
        "traversal",
        "GraphInput",
        "node-sequence",
        "all reachable nodes in distance order",
        "O(V + E log E)",
        "O(V+E)",
        conditions=("directed unweighted graph; unique edges",),
    ),
    _descriptor(
        "shortest-path",
        "path",
        "PathInput",
        "node-sequence-or-null",
        "valid minimum-edge path or null",
        "O(V + E log E)",
        "O(V+E)",
        conditions=("directed unweighted graph; unique edges",),
    ),
    _descriptor(
        "sorted-membership",
        "membership",
        "SearchInput",
        "boolean",
        "target occurs iff true",
        "O(n log n)",
        "O(n)",
        dependencies=("merge-sort", "binary-first"),
    ),
)
STRUCTURES = (
    DataStructureDescriptor(
        "af.integer-vector@1.0.0",
        (("index", "O(1)"), ("iterate", "O(n)")),
        "bounded immutable signed-64-bit sequence",
        True,
        False,
    ),
    DataStructureDescriptor(
        "af.fifo-deque@1.0.0",
        (("append", "O(1) amortized"), ("popleft", "O(1)")),
        "insertion-ordered single-owner queue",
        True,
        True,
    ),
    DataStructureDescriptor(
        "af.adjacency-list@1.0.0",
        (("build", "O(V+E log E)"), ("neighbors", "O(degree)")),
        "unique directed edges between declared nodes",
        True,
        True,
    ),
)
_NAMES = tuple(item.implementation_id.split("@")[0].split(".")[-1] for item in DESCRIPTORS)


class ReferenceAlgorithms:
    """Only compiled allowlisted implementations execute; snapshots cannot import code.

    Quarantine creates a new snapshot, preserving prior history. It cannot admit a new
    implementation. Installed source plus reviewed tests defines the initial trust set.
    """

    def __init__(self, *, quarantined: tuple[tuple[str, str], ...] = ()) -> None:
        if len(set(name for name, _ in quarantined)) != len(quarantined):
            raise AlgorithmError("duplicate quarantine entry")
        for name, reason in quarantined:
            if name not in _NAMES or not isinstance(reason, str) or not 1 <= len(reason) <= 200:
                raise AlgorithmError("invalid quarantine record")
        self._quarantined = tuple(sorted(quarantined))

    @property
    def structures(self) -> tuple[DataStructureDescriptor, ...]:
        return STRUCTURES

    @property
    def digest(self) -> str:
        return digest(parse_json(self.snapshot()))

    def descriptor(self, name: str) -> AlgorithmDescriptor:
        try:
            return DESCRIPTORS[_NAMES.index(name)]
        except ValueError as exc:
            raise AlgorithmError("unknown algorithm") from exc

    def snapshot(self) -> bytes:
        return canonical(
            {
                "schema": "af.algorithms.snapshot@1",
                "version": VERSION,
                "algorithms": [asdict(item) for item in DESCRIPTORS],
                "input_schemas": {
                    model.__name__: model.model_json_schema() for model in MODELS.values()
                },
                "structures": [asdict(item) for item in STRUCTURES],
                "quarantined": self._quarantined,
            }
        )

    @classmethod
    def from_snapshot(cls, data: bytes) -> ReferenceAlgorithms:
        raw = parse_json(data)
        if not isinstance(raw, dict) or not isinstance(raw.get("quarantined"), list):
            raise AlgorithmError("unsupported catalogue snapshot")
        records = raw["quarantined"]
        if any(
            not isinstance(row, list)
            or len(row) != 2
            or not all(isinstance(item, str) for item in row)
            for row in records
        ):
            raise AlgorithmError("invalid quarantine snapshot")
        result = cls(quarantined=tuple((row[0], row[1]) for row in records))
        if canonical(raw) != result.snapshot():
            raise AlgorithmError("snapshot is not an installed compatible catalogue")
        return result

    def quarantine(self, name: str, *, reason: str) -> ReferenceAlgorithms:
        records = dict(self._quarantined)
        records[name] = reason
        return ReferenceAlgorithms(quarantined=tuple(records.items()))

    def execute(
        self, name: str, data: bytes, *, expected_snapshot: str | None = None
    ) -> AlgorithmResult:
        if expected_snapshot is not None and expected_snapshot != self.digest:
            raise AlgorithmError("catalogue snapshot mismatch")
        descriptor = self.descriptor(name)
        blocked = dict(self._quarantined)
        if name in blocked or any(dep in blocked for dep in descriptor.dependencies):
            raise AlgorithmError("implementation or dependency is quarantined")
        request = parse_input(name, data)
        value = evaluate(request)
        if not verify_output(request, value):
            raise AlgorithmError("independent verification failed")
        return AlgorithmResult(
            name, request.digest, self.digest, digest(asdict(descriptor)), canonical(value)
        )

    def select(self, requirements: AlgorithmRequirements) -> SelectionPlan:
        def plan(
            status: str, chosen: str | None, eligible: tuple[str, ...], reason: str
        ) -> SelectionPlan:
            # Status literals are kept explicit at the public boundary below.
            if status == "selected":
                return SelectionPlan("selected", chosen, eligible, (reason,), self.digest)
            if status == "needs_information":
                return SelectionPlan("needs_information", None, eligible, (reason,), self.digest)
            if status == "needs_evidence":
                return SelectionPlan("needs_evidence", None, eligible, (reason,), self.digest)
            return SelectionPlan("no_route", None, eligible, (reason,), self.digest)

        blocked = dict(self._quarantined)
        eligible = tuple(
            name
            for name, item in zip(_NAMES, DESCRIPTORS, strict=True)
            if item.family == requirements.family
            and name not in blocked
            and not any(dep in blocked for dep in item.dependencies)
            and (requirements.stable is not True or item.stable is True)
        )
        if not eligible or (requirements.max_items is not None and requirements.max_items > 1024):
            return plan("no_route", None, eligible, "unsupported_contract")
        if requirements.family in {"search", "matching", "path", "traversal"}:
            return plan("needs_information", None, eligible, "structured_preconditions_unresolved")
        if requirements.max_items is None:
            return plan("needs_information", None, eligible, "input_bound_unknown")
        if requirements.max_memory_bytes is not None:
            return plan("needs_evidence", None, eligible, "memory_measurement_unavailable")
        preference = "insertion-sort" if requirements.max_items <= 32 else "merge-sort"
        chosen = preference if preference in eligible else eligible[0]
        return plan("selected", chosen, eligible, "bounded_reference_policy_not_benchmark_ranking")
