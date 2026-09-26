"""Contracts for bounded local capability engineering and atomic promotion."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Final, Literal, Protocol, Self

from pydantic import Field, model_validator

from af.framework.context import ExecutionContext
from af.framework.workflows import CheckpointModel, WorkflowComponent, WorkflowDefinition

FAMILY: Final = "text.nonblank-line-total"


class EngineeringRequest(CheckpointModel):
    family: Literal["text.nonblank-line-total"] = FAMILY
    texts: tuple[str, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def bounded_input(self) -> Self:
        if sum(len(text.encode()) for text in self.texts) > 60_000:
            raise ValueError("engineering request data exceeds its bound")
        if len(json.dumps({"texts": self.texts}, ensure_ascii=False).encode()) > 65_536:
            raise ValueError("engineering request encoded data exceeds its bound")
        return self


class EngineeringRouteEvidence(CheckpointModel):
    status: Literal[
        "no-route-exhausted",
        "available",
        "denied",
        "needs-clarification",
        "cancelled",
        "unavailable",
    ]
    history: tuple[str, ...] = Field(min_length=1, max_length=256)


class EngineeringRouteProbe(Protocol):
    def probe(
        self, request: EngineeringRequest, context: ExecutionContext
    ) -> EngineeringRouteEvidence: ...


class EngineeringGate(CheckpointModel):
    name: str
    passed: bool
    evidence_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    elapsed_seconds: float = Field(ge=0)


class ComponentAdmission(CheckpointModel):
    family: Literal["text.nonblank-line-total"] = FAMILY
    version: Literal["1.0.0"] = "1.0.0"
    component_id: Literal["text.nonblank-line-count-batch"] = "text.nonblank-line-count-batch"
    component_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    input_schema: Literal["text.line-batch.v1"] = "text.line-batch.v1"
    output_schema: Literal["number.vector.v1"] = "number.vector.v1"
    classifier_prefix: Literal["count nonblank lines:"] = "count nonblank lines:"
    artifact_path: str
    worktree: str
    branch: str
    source_head: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    candidate_head: str = Field(pattern=r"^[a-f0-9]{40,64}$")
    gates: tuple[EngineeringGate, ...]
    reviewer: str
    created_at: datetime
    definition: WorkflowDefinition
    lower_route_history: tuple[str, ...] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def full_admission(self) -> Self:
        required = {
            "test-first",
            "tests",
            "lint",
            "format",
            "typing",
            "coverage",
            "security",
            "compatibility",
            "governance",
            "independent-review",
        }
        if (
            len(self.gates) != len(required)
            or {gate.name for gate in self.gates} != required
            or not all(gate.passed for gate in self.gates)
        ):
            raise ValueError("component admission requires all independent engineering gates")
        if not self.reviewer or self.created_at.utcoffset() is None:
            raise ValueError("component admission requires reviewer and aware timestamp")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class EngineeringCompletion(CheckpointModel):
    output: int = Field(ge=0)
    run_id: str
    engineered: bool
    engineering_seconds: float = Field(ge=0)
    execution_seconds: float = Field(ge=0)
    model_calls: Literal[0] = 0
    admission: ComponentAdmission


class EngineeringCatalogue(Protocol):
    def lookup(self, family: str) -> ComponentAdmission | None: ...
    def claim(self, family: str, owner: str) -> bool: ...
    def status(self, family: str) -> str | None: ...
    def admit(self, admission: ComponentAdmission, owner: str) -> None: ...
    def reject(self, family: str, owner: str, reason: str) -> None: ...
    def invalidate(self, family: str, reason: str) -> None: ...


class LocalComponentEngineer(Protocol):
    def engineer(
        self, family: str, context: ExecutionContext, *, route_history: tuple[str, ...]
    ) -> ComponentAdmission: ...
    def component(self, admission: ComponentAdmission) -> WorkflowComponent: ...
