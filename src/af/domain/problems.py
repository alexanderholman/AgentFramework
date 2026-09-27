"""Finite, frame-relative computational problems, separate from their solution methods.

Only compiled decomposition rules are admitted. Parsing or matching a descriptor grants
no execution authority; the normal AF procedure/policy pipeline remains the executor.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from af.domain.algorithms.contracts import (
    VERSION,
    AlgorithmError,
    AlgorithmResult,
    SearchInput,
    canonical,
    digest,
    parse_input,
    parse_json,
)
from af.domain.algorithms.implementations import verify_output


@dataclass(frozen=True, slots=True)
class OntologyFrame:
    ontology: str = "af.closed-computational-problems@1.0.0"
    decomposition_rules: str = "af.finite.problem-rules@1.0.0"
    abstraction: str = "bounded exact input-output contracts"
    evidence_snapshot: str = digest("project-authored-prime-contracts@1.0.0")


@dataclass(frozen=True, slots=True)
class ProblemDescriptor:
    id: str
    objective: str
    input_schema: str
    output_schema: str
    acceptance: str
    prime: bool = True
    frame: OntologyFrame = OntologyFrame()
    version: str = VERSION
    source: str = "project-authored independent problem abstraction"
    rights: str = "LicenseRef-AF-Repository"
    effects: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return digest(asdict(self))


_DESCRIPTORS = (
    ProblemDescriptor(
        "ordering",
        "construct an ascending permutation",
        "integer-sequence",
        "sorted-integer-sequence",
        "ascending order and multiset equal to input",
    ),
    ProblemDescriptor(
        "sorted-position",
        "find first position of target",
        "sorted-integer-sequence+target",
        "index-or-null",
        "minimal matching index; null iff target absent",
    ),
    ProblemDescriptor(
        "integer-membership",
        "decide whether target occurs",
        "integer-sequence+target",
        "boolean",
        "true iff any input equals target",
    ),
    ProblemDescriptor(
        "integer-sum",
        "aggregate integer sum",
        "integer-sequence",
        "integer",
        "exact mathematical sum of original inputs",
    ),
    ProblemDescriptor(
        "substring-occurrences",
        "enumerate overlapping literal occurrences",
        "text+nonempty-pattern",
        "index-sequence",
        "every and only matching Unicode codepoint offset; no normalization",
    ),
    ProblemDescriptor(
        "reachable-nodes",
        "enumerate reachable graph nodes",
        "directed-unweighted-graph+start",
        "node-sequence",
        "each reachable node exactly once in nondecreasing distance",
    ),
    ProblemDescriptor(
        "shortest-unweighted-path",
        "find a minimum edge path",
        "directed-unweighted-graph+start+target",
        "node-sequence-or-null",
        "valid shortest path; null iff unreachable",
    ),
    ProblemDescriptor(
        "order-and-locate",
        "order values and locate first target position",
        "integer-sequence+target",
        "ordered+index",
        "ordered permutation of original input and first target index in that permutation",
        prime=False,
    ),
)
_BY_ID = {item.id: item for item in _DESCRIPTORS}
_ALGORITHMS = {
    "ordering": "merge-sort",
    "sorted-position": "binary-first",
    "integer-membership": "linear-membership",
    "integer-sum": "sum-integers",
    "substring-occurrences": "string-matches",
    "reachable-nodes": "breadth-first",
    "shortest-unweighted-path": "shortest-path",
}
_ALIASES = {"sort": "ordering", "contains": "integer-membership", "sum": "integer-sum"}


@dataclass(frozen=True, slots=True)
class ProblemNode:
    id: str
    problem: str
    input_from: str | None = None

    @property
    def depends_on(self) -> tuple[str, ...]:
        return () if self.input_from is None else (self.input_from,)


@dataclass(frozen=True, slots=True)
class ProblemGraph:
    nodes: tuple[ProblemNode, ...]
    output: str

    def __post_init__(self) -> None:
        by_id = {node.id: node for node in self.nodes}
        if (
            not 1 <= len(self.nodes) <= 16
            or len(by_id) != len(self.nodes)
            or self.output not in by_id
        ):
            raise AlgorithmError("invalid problem graph identity or bound")
        for node in self.nodes:
            if not node.id or len(node.id) > 64 or node.problem not in _ALGORITHMS:
                raise AlgorithmError("unknown problem node")
            if node.input_from is not None:
                source = by_id.get(node.input_from)
                if (
                    source is None
                    or source.problem != "ordering"
                    or node.problem != "sorted-position"
                ):
                    raise AlgorithmError("problem edge schemas do not unify")
        visited: set[str] = set()
        cursor: str | None = self.output
        while cursor is not None:
            if cursor in visited:
                raise AlgorithmError("cyclic problem graph")
            visited.add(cursor)
            cursor = by_id[cursor].input_from
        if visited != set(by_id):
            raise AlgorithmError("orphan problem graph node")

    @property
    def digest(self) -> str:
        return digest(
            {
                "nodes": [asdict(node) for node in sorted(self.nodes, key=lambda n: n.id)],
                "output": self.output,
                "rule_version": "af.finite.problem-rules@1.0.0",
            }
        )


class AlgorithmPort(Protocol):
    @property
    def digest(self) -> str: ...
    def execute(
        self, name: str, data: bytes, *, expected_snapshot: str | None = None
    ) -> AlgorithmResult: ...


@dataclass(frozen=True, slots=True)
class ProblemHypothesis:
    status: Literal["matched", "no_match", "under_specified", "invalid"]
    problem_id: str | None
    match_type: Literal["exact", "declared_alias", "none"]
    openings: tuple[str, ...] = ()
    input_digest: str | None = None


class ProblemCatalogue:
    def __init__(self, algorithms: AlgorithmPort) -> None:
        self._algorithms = algorithms

    @staticmethod
    def descriptor(name: str) -> ProblemDescriptor:
        try:
            return _BY_ID[name]
        except KeyError as exc:
            raise AlgorithmError("unknown canonical problem") from exc

    @staticmethod
    def snapshot() -> bytes:
        return canonical(
            {
                "schema": "af.problems.snapshot@1",
                "descriptors": [asdict(item) for item in _DESCRIPTORS],
                "aliases": _ALIASES,
                "algorithm_bindings": _ALGORITHMS,
                "composite": {"order-and-locate": ["ordering", "sorted-position"]},
            }
        )

    @classmethod
    def from_snapshot(cls, data: bytes, algorithms: AlgorithmPort) -> ProblemCatalogue:
        if canonical(parse_json(data)) != cls.snapshot():
            raise AlgorithmError("problem snapshot is not the installed ontology")
        return cls(algorithms)

    @staticmethod
    def decompose(name: str) -> ProblemGraph:
        ProblemCatalogue.descriptor(name)
        if name == "order-and-locate":
            return ProblemGraph(
                (
                    ProblemNode("order", "ordering"),
                    ProblemNode("locate", "sorted-position", "order"),
                ),
                "locate",
            )
        return ProblemGraph((ProblemNode("result", name),), "result")

    @staticmethod
    def match(name: str, data: bytes) -> ProblemHypothesis:
        canonical_name = _ALIASES.get(name, name)
        if canonical_name not in _BY_ID:
            return ProblemHypothesis("no_match", None, "none", ("supported_problem_id",))
        method = _ALGORITHMS.get(canonical_name, "linear-membership")
        try:
            raw = parse_json(data)
            fields = {"values"}
            if method in {"binary-first", "linear-membership"}:
                fields.add("target")
            elif method == "string-matches":
                fields = {"text", "pattern"}
            elif method in {"breadth-first", "shortest-path"}:
                fields = {"nodes", "edges", "start"}
                if method == "shortest-path":
                    fields.add("target")
            if isinstance(raw, dict) and fields - raw.keys():
                return ProblemHypothesis(
                    "under_specified", canonical_name, "none", tuple(sorted(fields - raw.keys()))
                )
            parsed = parse_input(method, data)
        except AlgorithmError:
            return ProblemHypothesis("invalid", canonical_name, "none", ("invalid_arguments",))
        return ProblemHypothesis(
            "matched",
            canonical_name,
            "exact" if name == canonical_name else "declared_alias",
            input_digest=parsed.digest,
        )

    def resolve(self, name: str, data: bytes) -> dict[str, object]:
        descriptor = self.descriptor(name)
        graph = self.decompose(name)
        snapshot = self._algorithms.digest
        nodes: list[dict[str, object]] = []
        if name == "order-and-locate":
            request = parse_input("linear-membership", data)
            assert isinstance(request.arguments, SearchInput)
            args = request.arguments
            ordered = self._algorithms.execute(
                "merge-sort", canonical({"values": args.values}), expected_snapshot=snapshot
            )
            nodes.append(ordered.to_dict())
            found = self._algorithms.execute(
                "binary-first",
                canonical({"values": ordered.value, "target": args.target}),
                expected_snapshot=snapshot,
            )
            nodes.append(found.to_dict())
            value: object = {"ordered": ordered.value, "index": found.value}
        else:
            result = self._algorithms.execute(_ALGORITHMS[name], data, expected_snapshot=snapshot)
            nodes.append(result.to_dict())
            value = result.value
        if not self.verify(name, data, value):
            raise AlgorithmError("whole problem acceptance failed")
        return {
            "problem": name,
            "problem_digest": descriptor.digest,
            "graph_digest": graph.digest,
            "ontology": descriptor.frame.ontology,
            "algorithms_snapshot": snapshot,
            "nodes": nodes,
            "value": value,
            "verified": True,
        }

    @staticmethod
    def verify(name: str, data: bytes, value: object) -> bool:
        ProblemCatalogue.descriptor(name)
        if name != "order-and-locate":
            return verify_output(parse_input(_ALGORITHMS[name], data), value)
        original = parse_input("linear-membership", data)
        assert isinstance(original.arguments, SearchInput)
        args = original.arguments
        if not isinstance(value, dict) or set(value) != {"ordered", "index"}:
            return False
        if not verify_output(
            parse_input("merge-sort", canonical({"values": args.values})), value["ordered"]
        ):
            return False
        return verify_output(
            parse_input(
                "binary-first", canonical({"values": value["ordered"], "target": args.target})
            ),
            value["index"],
        )
