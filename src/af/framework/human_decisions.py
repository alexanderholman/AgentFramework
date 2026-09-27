"""Durable human decisions narrow a pinned request; they never grant general authority."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from af.framework.errors import AFError


class HumanDecisionError(AFError):
    def __init__(self) -> None:
        super().__init__("human decision is unavailable or no longer authorized")


class HumanDecisionKind(StrEnum):
    CLARIFICATION = "NeedsClarification"
    CONFIRMATION = "NeedsConfirmation"
    DENIED = "Denied"
    NO_ROUTE = "NoRoute"
    COMPLETED = "Completed"


class DecisionModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class HumanChoice(DecisionModel):
    choice_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    consequence: str = Field(min_length=1, max_length=256)
    procedure_id: str = Field(min_length=1, max_length=64)
    procedure_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    intent: str = Field(min_length=1, max_length=64)
    execution_prompt: str = Field(min_length=1, max_length=10_000)
    arguments: dict[str, str] = Field(max_length=16)


class PendingHumanDecision(DecisionModel):
    schema_version: Literal["af.human-decision/1"] = "af.human-decision/1"
    decision_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: HumanDecisionKind
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    authority_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    policy_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    response_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    choices: tuple[HumanChoice, ...] = Field(min_length=1, max_length=16)
    created_at: float
    expires_at: float

    @model_validator(mode="after")
    def pending_contract(self) -> Self:
        if (
            self.kind not in {HumanDecisionKind.CLARIFICATION, HumanDecisionKind.CONFIRMATION}
            or not self.created_at < self.expires_at <= self.created_at + 3600
            or len({choice.choice_id for choice in self.choices}) != len(self.choices)
        ):
            raise ValueError("invalid pending human decision contract")
        return self


class HumanDecisionRepository(Protocol):
    def create(self, decision: PendingHumanDecision) -> None: ...
    def get(self, decision_id: str) -> PendingHumanDecision: ...
    def claim(self, decision: PendingHumanDecision) -> None: ...
    def finish(self, decision_id: str, status: str) -> None: ...
    def revoke(self, decision: PendingHumanDecision) -> None: ...
