"""Transport-independent contracts for bounded, evidence-gated durable workflows.

Checkpoints contain minimized bytes supplied by the caller. A host must apply the
same retention/access policy to its workflow database as to the source data.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from af.framework.context import ExecutionContext
from af.framework.errors import AFError


class WorkflowError(AFError):
    """A workflow cannot proceed without changing or recovering its evidence."""


class WorkflowConflict(WorkflowError):
    """A newer fenced writer has already advanced this workflow."""


class NodeState(StrEnum):
    QUEUED = "queued"
    BLOCKED = "blocked"
    ADMITTED = "admitted"
    RUNNING = "running"
    CHECKPOINTING = "checkpointing"
    RETRYING = "retrying"
    COMPLETED = "completed"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INCONCLUSIVE = "inconclusive"
    NEEDS_HUMAN = "needs-human"


class CheckpointModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        ser_json_bytes="base64",
        val_json_bytes="base64",
        allow_inf_nan=False,
    )


class EvidenceRequirement(CheckpointModel):
    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    verifier: str = "component"
    minimum_bytes: int = Field(default=0, ge=0, le=65_536)


class WorkflowNode(CheckpointModel):
    node_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    component_id: str = Field(min_length=1, max_length=128)
    component_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    dependencies: tuple[EvidenceRequirement, ...] = ()


class WorkflowDefinition(CheckpointModel):
    workflow_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    compatibility_version: int = Field(default=1, ge=1)
    nodes: tuple[WorkflowNode, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        ids = {node.node_id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("workflow node IDs must be unique")
        pending = {
            node.node_id: {gate.node_id for gate in node.dependencies} for node in self.nodes
        }
        if any(not dependencies <= ids for dependencies in pending.values()):
            raise ValueError("workflow dependencies must name existing nodes")
        visited: set[str] = set()
        while pending:
            ready = {key for key, dependencies in pending.items() if dependencies <= visited}
            if not ready:
                raise ValueError("workflow graph must be acyclic")
            visited.update(ready)
            pending = {key: value for key, value in pending.items() if key not in ready}
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class WorkflowOutput(CheckpointModel):
    data: bytes = Field(max_length=65_536)


class NodeRecord(CheckpointModel):
    node_id: str
    state: NodeState
    attempt: int = Field(default=0, ge=0)
    fence: str | None = None
    lease_until: datetime | None = None
    output: bytes | None = Field(default=None, max_length=65_536)
    output_digest: str | None = None
    verifier: str | None = None
    verified: bool = False
    elapsed_seconds: float | None = Field(default=None, ge=0)
    reason: str | None = None


class WorkflowRun(CheckpointModel):
    run_id: str
    definition: WorkflowDefinition
    input_data: bytes = Field(max_length=65_536)
    input_digest: str
    authority_digest: str
    policy_version: str
    revision: int = Field(default=0, ge=0)
    nodes: tuple[NodeRecord, ...]
    created_at: datetime
    updated_at: datetime
    deadline: datetime | None = None
    cancelled: bool = False
    checkpoint_policy: str = "retained"


@dataclass(frozen=True, slots=True)
class WorkflowProgress:
    fraction: float
    states: tuple[tuple[str, NodeState], ...]
    eta_seconds: tuple[float, float] | None
    confidence: float
    assumptions: tuple[str, ...]
    observed_nodes: int
    updated_at: datetime
    stale: bool


class WorkflowHandler(Protocol):
    def execute(
        self,
        data: bytes,
        predecessors: Mapping[str, bytes],
        context: ExecutionContext,
        *,
        effect_key: str,
    ) -> WorkflowOutput: ...

    def verify(
        self, data: bytes, predecessors: Mapping[str, bytes], output: WorkflowOutput
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class WorkflowComponent:
    component_id: str
    version: str
    digest: str
    replay_safe: bool
    handler: WorkflowHandler

    def __post_init__(self) -> None:
        if (
            not self.component_id
            or not self.version
            or re.fullmatch(r"[a-f0-9]{64}", self.digest) is None
        ):
            raise ValueError("component requires identity, version and SHA-256 digest")


class WorkflowRepository(Protocol):
    def create(self, run: WorkflowRun) -> WorkflowRun: ...
    def get(self, run_id: str) -> WorkflowRun: ...
    def save(self, run: WorkflowRun, *, expected_revision: int) -> WorkflowRun: ...
    def history(self, run_id: str) -> tuple[WorkflowRun, ...]: ...


class WorkflowPolicy(Protocol):
    @property
    def version(self) -> str: ...
    def check(self, context: ExecutionContext, node: WorkflowNode, *, phase: str) -> None: ...


class WorkflowResources(Protocol):
    def acquire(
        self, context: ExecutionContext, node: WorkflowNode, *, effect_key: str
    ) -> str | None: ...
    def valid(self, lease: str) -> bool: ...
    def release(self, lease: str) -> None: ...
