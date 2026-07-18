"""Immutable values exchanged by AF contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum


class WorkTier(IntEnum):
    STATIC = 0
    PROCEDURAL = 1
    GENERATIVE = 2


class SelectorLocality(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


@dataclass(frozen=True, slots=True)
class WorkRequest:
    prompt: str
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Classification:
    intent: str
    arguments: dict[str, str]
    confidence: float
    classifier: str


@dataclass(frozen=True, slots=True)
class SelectionProposal:
    procedure_id: str
    intent: str
    arguments: dict[str, str]
    confidence: float


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    proposal: SelectionProposal | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ProcedureResult:
    output: str
    evidence: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    passed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class RouteMetrics:
    cost: int
    latency: int
    intelligence: int


@dataclass(frozen=True, slots=True)
class TraceEvent:
    stage: str
    detail: str


@dataclass(frozen=True, slots=True)
class RunTrace:
    id: str
    prompt: str
    intent: str | None
    arguments: dict[str, str]
    route: str | None
    tier: str | None
    output: str | None
    verified: bool
    reused: bool
    regression_passed: bool
    promoted: bool
    status: str
    events: tuple[TraceEvent, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    prompt: str
    output: str
    intent: str
    route: str
    tier: str
    status: str
    verified: bool
    reused: bool
    trace_id: str
