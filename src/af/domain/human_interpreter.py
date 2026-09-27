"""Immutable source, meaning, policy and release records for the finite human boundary."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol

from af.domain.algorithms.contracts import digest
from af.domain.ideons.records import ContextGraph, InterpretationRecord
from af.framework.errors import AFError

MAX_SIGNAL_BYTES = 4096


class InterpreterError(AFError, ValueError):
    """Interpretation cannot cross a source, policy, evidence or release boundary."""


def _reference(value: str) -> None:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}", value) is None
    ):
        raise InterpreterError("invalid bounded reference")


class SourceKind(StrEnum):
    USER = "user"
    DOCUMENT = "document"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ObservedSignal:
    data: bytes
    source_id: str
    source_kind: SourceKind
    received_at: datetime
    rights: str

    def __post_init__(self) -> None:
        _reference(self.source_id)
        _reference(self.rights)
        if not isinstance(self.source_kind, SourceKind):
            raise InterpreterError("source kind must come from the host boundary")
        if not isinstance(self.data, bytes) or not 1 <= len(self.data) <= MAX_SIGNAL_BYTES:
            raise InterpreterError("signal byte bound exceeded")
        if not isinstance(self.received_at, datetime) or self.received_at.tzinfo is None:
            raise InterpreterError("observation requires an aware timestamp")
        try:
            text = self.data.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise InterpreterError("lossless UTF-8 decoding required") from exc
        if not text.strip() or "\x00" in text:
            raise InterpreterError("empty or NUL-containing signal")

    @property
    def digest(self) -> str:
        return digest(
            {
                "content_sha256": hashlib.sha256(self.data).hexdigest(),
                "source_id": self.source_id,
                "source_kind": self.source_kind,
                "received_at": self.received_at.astimezone(UTC).isoformat(),
                "rights": self.rights,
            }
        )


@dataclass(frozen=True, slots=True)
class SourceSpan:
    field: str
    codepoint_span: tuple[int, int]
    byte_span: tuple[int, int]
    surface: str


@dataclass(frozen=True, slots=True)
class WhatContract:
    problem: str
    arguments_json: bytes
    observation_digest: str
    template_id: str
    template_digest: str
    provenance: tuple[SourceSpan, ...]

    @property
    def meaning_digest(self) -> str:
        return digest({"problem": self.problem, "arguments": self.arguments_json.decode()})

    def to_dict(self) -> dict[str, object]:
        return {
            "problem": self.problem,
            "arguments_json": self.arguments_json.decode(),
            "observation_digest": self.observation_digest,
            "template_id": self.template_id,
            "template_digest": self.template_digest,
            "provenance": [asdict(span) for span in self.provenance],
        }

    @property
    def digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class HumanInterpretation:
    observation: ObservedSignal
    ideon: InterpretationRecord
    state: Literal["accepted", "quarantined", "rejected", "needs_clarification"]
    what: WhatContract | None
    openings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "af.human-interpretation@1",
            "observation_digest": self.observation.digest,
            "state": self.state,
            "what": self.what.to_dict() if self.what else None,
            "openings": list(self.openings),
            "ideon": self.ideon.to_dict(),
            "execution_authority": False,
            "normalization": "original preserved; Ideon profile applies only to lookup",
        }

    @property
    def digest(self) -> str:
        return digest(self.to_dict())


@dataclass(frozen=True, slots=True)
class ShouldDecision:
    what_digest: str
    status: Literal["permit", "deny", "clarify"]
    policy_id: str
    reason: str

    def __post_init__(self) -> None:
        for ref in (self.what_digest, self.policy_id, self.reason):
            _reference(ref)
        if self.status not in {"permit", "deny", "clarify"}:
            raise InterpreterError("unsupported SHOULD verdict")


@dataclass(frozen=True, slots=True)
class HowAssessment:
    what_digest: str
    feasible: bool | None
    capabilities: tuple[str, ...]
    reason: str

    def __post_init__(self) -> None:
        for ref in (self.what_digest, self.reason, *self.capabilities):
            _reference(ref)
        if self.feasible is not None and type(self.feasible) is not bool:
            raise InterpreterError("feasibility must preserve unknown")


class ShouldPort(Protocol):
    def assess(self, what: WhatContract, context: ContextGraph) -> ShouldDecision: ...


class HowPort(Protocol):
    def assess(self, what: WhatContract, context: ContextGraph) -> HowAssessment: ...


@dataclass(frozen=True, slots=True)
class MaskAssessment:
    what_digest: str
    should: ShouldDecision
    how: HowAssessment | None


@dataclass(frozen=True, slots=True)
class OutboundClaim:
    what_digest: str
    problem: str
    verified_value_json: bytes
    verifier_id: str = "af.problem-whole-acceptance@1.0.0"

    @property
    def digest(self) -> str:
        return digest(
            {
                "what_digest": self.what_digest,
                "problem": self.problem,
                "verified_value": self.verified_value_json.decode(),
                "verifier": self.verifier_id,
            }
        )


@dataclass(frozen=True, slots=True)
class ReleaseDecision:
    claim_digest: str
    audience: str
    permitted: bool
    policy_id: str

    def __post_init__(self) -> None:
        for ref in (self.claim_digest, self.audience, self.policy_id):
            _reference(ref)
        if type(self.permitted) is not bool:
            raise InterpreterError("release permission must be explicit")


class ReleasePort(Protocol):
    def decide(self, claim: OutboundClaim, audience: str) -> ReleaseDecision: ...


@dataclass(frozen=True, slots=True)
class RenderedSignal:
    data: bytes
    audience: str
    evidence_digest: str
    release_policy: str
    template: str = "af.verified-problem-value@1.0.0"
    losses: tuple[str, ...] = ()
