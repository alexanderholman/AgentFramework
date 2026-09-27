"""Finite host-admitted local development units and durable execution receipts."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Protocol, Self

from pydantic import Field, model_validator

from af.framework.context import ExecutionContext
from af.framework.engineering import EngineeringCompletion, EngineeringRequest
from af.framework.self_development import CyclePlan, DevelopmentSnapshot, SelectionPolicy
from af.framework.workflows import CheckpointModel


class LocalDevelopmentUnit(CheckpointModel):
    """Host configuration grants only the installed finite engineering family."""

    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}:[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    expected_head: str = Field(pattern=r"^[a-f0-9]{40}$")
    issued_at: datetime
    expires_at: datetime
    request: EngineeringRequest

    @model_validator(mode="after")
    def bounded_authority(self) -> Self:
        if self.issued_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("development authority times must be timezone aware")
        if not timedelta(0) < self.expires_at - self.issued_at <= timedelta(hours=1):
            raise ValueError("development authority must expire within one hour")
        return self

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class DevelopmentPlanningInput(CheckpointModel):
    snapshot: DevelopmentSnapshot
    policy: SelectionPolicy
    unit: LocalDevelopmentUnit | None = None


class PreparedDevelopment(CheckpointModel):
    inputs: DevelopmentPlanningInput
    plan: CyclePlan
    plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    authority_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def bound_plan(self) -> Self:
        if (
            self.plan.snapshot_digest != self.inputs.snapshot.digest
            or self.plan.digest != self.plan_digest
        ):
            raise ValueError("development plan receipt is misbound")
        return self


class DevelopmentCompletion(CheckpointModel):
    plan_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    completion: EngineeringCompletion


class DevelopmentSource(Protocol):
    def capture(self, context: ExecutionContext) -> DevelopmentPlanningInput: ...


class DevelopmentExecutor(Protocol):
    def execute(
        self, prepared: PreparedDevelopment, context: ExecutionContext
    ) -> EngineeringCompletion: ...


class DevelopmentLedger(Protocol):
    def save(self, prepared: PreparedDevelopment) -> None: ...
    def load(self, digest: str) -> PreparedDevelopment: ...
    def completed(self, digest: str) -> DevelopmentCompletion | None: ...
    def claim(self, digest: str) -> bool: ...
    def finish(self, completion: DevelopmentCompletion) -> None: ...
