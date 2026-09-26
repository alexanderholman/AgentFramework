"""Finite, strict algorithm contracts; source data never contains executable code."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from af.framework.errors import AFError

VERSION = "1.0.0"
MAX_INPUT_BYTES = 32768
MAX_ITEMS = 1024
MAX_NODES = 64
MAX_EDGES = 512


class AlgorithmError(AFError, ValueError):
    """A bounded catalogue request cannot be safely resolved."""


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise AlgorithmError("duplicate field")
        result[name] = value
    return result


def parse_json(data: bytes) -> object:
    if not isinstance(data, bytes) or len(data) > MAX_INPUT_BYTES:
        raise AlgorithmError("input size limit exceeded")
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise AlgorithmError("invalid catalogue JSON") from exc


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


Integer = Annotated[int, Field(ge=-(2**63), le=2**63 - 1)]
Values = Annotated[tuple[Integer, ...], Field(max_length=MAX_ITEMS)]
Node = Annotated[int, Field(ge=0, lt=MAX_NODES)]


class SequenceInput(FrozenModel):
    values: Values


class SearchInput(SequenceInput):
    target: Integer


class StringInput(FrozenModel):
    text: Annotated[str, Field(max_length=2048)]
    pattern: Annotated[str, Field(min_length=1, max_length=256)]


class GraphInput(FrozenModel):
    nodes: Annotated[int, Field(ge=1, le=MAX_NODES)]
    edges: Annotated[tuple[tuple[Node, Node], ...], Field(max_length=MAX_EDGES)]
    start: Node

    @model_validator(mode="after")
    def validate_graph(self) -> GraphInput:
        if self.start >= self.nodes or any(max(edge) >= self.nodes for edge in self.edges):
            raise ValueError("graph refers to absent node")
        if len(set(self.edges)) != len(self.edges):
            raise ValueError("duplicate graph edge")
        return self


class PathInput(GraphInput):
    target: Node

    @model_validator(mode="after")
    def validate_target(self) -> PathInput:
        if self.target >= self.nodes:
            raise ValueError("graph refers to absent target")
        return self


type Input = SequenceInput | SearchInput | StringInput | GraphInput | PathInput
MODELS: dict[str, type[SequenceInput] | type[StringInput] | type[GraphInput]] = {
    "insertion-sort": SequenceInput,
    "merge-sort": SequenceInput,
    "binary-first": SearchInput,
    "linear-membership": SearchInput,
    "sorted-membership": SearchInput,
    "sum-integers": SequenceInput,
    "string-matches": StringInput,
    "breadth-first": GraphInput,
    "shortest-path": PathInput,
}


@dataclass(frozen=True, slots=True)
class AlgorithmInput:
    algorithm: str
    arguments: Input

    @property
    def digest(self) -> str:
        return digest(
            {"algorithm": self.algorithm, "arguments": self.arguments.model_dump(mode="json")}
        )


def parse_input(algorithm: str, data: bytes) -> AlgorithmInput:
    if algorithm not in MODELS:
        raise AlgorithmError("unknown installed algorithm")
    raw = parse_json(data)
    try:
        value = MODELS[algorithm].model_validate_json(canonical(raw))
    except (ValidationError, ValueError) as exc:
        raise AlgorithmError("invalid algorithm arguments") from exc
    if algorithm == "binary-first":
        assert isinstance(value, SearchInput)
        if any(a > b for a, b in zip(value.values, value.values[1:], strict=False)):
            raise AlgorithmError("binary search requires ascending values")
    return AlgorithmInput(algorithm, value)


@dataclass(frozen=True, slots=True)
class Provenance:
    source: str = "AF independently authored reference implementation"
    revision: str = VERSION
    code_license: str = "LicenseRef-AF-Repository"
    fixtures_license: str = "LicenseRef-AF-Repository"
    concept_license: str | None = None
    locator: str = "src/af/domain/algorithms/implementations.py"


@dataclass(frozen=True, slots=True)
class Complexity:
    worst_time: str
    auxiliary_space: str
    expected_time: str | None = None
    amortized_time: str | None = None
    measured_latency_ns: int | None = None
    measured_peak_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class AlgorithmDescriptor:
    algorithm_id: str
    family: str
    implementation_id: str
    input_schema: str
    output_schema: str
    preconditions: tuple[str, ...]
    postcondition: str
    complexity: Complexity
    stable: bool | None = None
    exact: bool = True
    deterministic: bool = True
    effects: tuple[str, ...] = ()
    version: str = VERSION
    provenance: Provenance = Provenance()
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DataStructureDescriptor:
    id: str
    operations: tuple[tuple[str, str], ...]
    invariant: str
    ordered: bool
    mutable: bool
    durable: bool = False
    thread_safe: bool | None = None
    version: str = VERSION


@dataclass(frozen=True, slots=True)
class AlgorithmRequirements:
    family: str
    max_items: int | None = None
    stable: bool | None = None
    max_memory_bytes: int | None = None

    def __post_init__(self) -> None:
        if self.stable is not None and type(self.stable) is not bool:
            raise AlgorithmError("stability constraint must be boolean or unknown")
        for value in (self.max_items, self.max_memory_bytes):
            if value is not None and (type(value) is not int or value < 0):
                raise AlgorithmError("resource bounds must be nonnegative integers or unknown")


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    status: Literal["selected", "needs_information", "needs_evidence", "no_route"]
    chosen: str | None
    eligible: tuple[str, ...]
    reasons: tuple[str, ...]
    catalogue_digest: str


@dataclass(frozen=True, slots=True)
class AlgorithmResult:
    algorithm: str
    input_digest: str
    catalogue_digest: str
    descriptor_digest: str
    value_json: bytes
    verified: bool = True
    verifier: str = "af.reference.independent-oracles@1.0.0"

    @property
    def value(self) -> object:
        return json.loads(self.value_json)

    def to_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "input_digest": self.input_digest,
            "catalogue_digest": self.catalogue_digest,
            "descriptor_digest": self.descriptor_digest,
            "value": self.value,
            "verified": self.verified,
            "verifier": self.verifier,
        }
