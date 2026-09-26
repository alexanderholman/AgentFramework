"""Immutable values exchanged by AF contracts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from math import isfinite

from af.framework.errors import InvalidPrompt

# Shared user-prompt ceiling enforced by the application boundary and reflected by adapters.
PROMPT_MAX_LENGTH = 10_000
TASK_LIST_DEFAULT_LIMIT = 50
TASK_LIST_MAX_LIMIT = 100
TASK_LIST_MAX_OFFSET = 10_000
PROCEDURE_ARGUMENT_COUNT_MAX = 16
WORK_REQUEST_ATTRIBUTE_COUNT_MAX = 16
WORK_REQUEST_ATTRIBUTE_KEY_MAX_LENGTH = 64
WORK_REQUEST_ATTRIBUTE_NUMBER_MAX_LENGTH = 64
ROUTE_METRIC_MAX = 10_000
SELECTOR_PROVIDER_ID_MAX_LENGTH = 64
CLASSIFIER_ID_MAX_LENGTH = 64
PROCEDURE_ID_MAX_LENGTH = 64
PROCEDURE_VERSION_MAX_LENGTH = 32
PROCEDURE_INTENT_MAX_LENGTH = 64
ARGUMENT_NAME_MAX_LENGTH = 32

_SELECTOR_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,63})$")
_CLASSIFIER_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,63})$")
_PROCEDURE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,63})$")
_PROCEDURE_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}$")
_PROCEDURE_INTENT_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_ARGUMENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class WorkTier(IntEnum):
    STATIC = 0
    PROCEDURAL = 1


class SelectorLocality(StrEnum):
    LOCAL = "local"
    REMOTE = "remote"


class ArgumentValueType(StrEnum):
    TEXT = "text"


class ProcedureEffect(StrEnum):
    READ_ONLY = "read_only"
    SIDE_EFFECT = "side_effect"


class ProcedureRisk(StrEnum):
    NONE = "none"
    ELEVATED = "elevated"


class SelectionHandoffReason(StrEnum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"


class SelectionReasonCode(StrEnum):
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    MALFORMED = "malformed"
    STALE_PROCEDURE = "stale_procedure"
    AMBIGUOUS = "ambiguous"
    APPROVED = "approved"
    BELOW_THRESHOLD = "below_threshold"
    GROUNDING_FAILED = "grounding_failed"
    GROUNDING_UNAVAILABLE = "grounding_unavailable"
    INVALID_ARGUMENTS = "invalid_arguments"
    INVALID_CONFIDENCE = "invalid_confidence"
    INVALID_DECISION = "invalid_decision"
    NO_SELECTOR = "no_selector"
    POLICY_DENIED = "policy_denied"
    REMOTE_SELECTION = "remote_selection"
    SELECTOR_FAILURE = "selector_failure"
    UNAVAILABLE = "unavailable"
    UNKNOWN_PROCEDURE = "unknown_procedure"
    UNSUPPORTED = "unsupported"
    UNSUPPORTED_INTENT = "unsupported_intent"


@dataclass(frozen=True, slots=True)
class SelectorMetadata:
    provider_id: str
    locality: SelectorLocality


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
    version: str | None = None
    digest: str | None = None


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    proposal: SelectionProposal | None = None
    reason: SelectionHandoffReason | None = None


@dataclass(frozen=True, slots=True)
class ProcedureArgumentDescriptor:
    """Selector-facing schema for one trusted textual procedure argument."""

    name: str
    value_type: ArgumentValueType
    min_length: int = 0
    max_length: int | None = None
    allow_blank: bool = False


@dataclass(frozen=True, slots=True)
class ProcedureDescriptor:
    procedure_id: str
    version: str
    intent: str
    arguments: tuple[ProcedureArgumentDescriptor, ...]
    effect: ProcedureEffect
    risk: ProcedureRisk
    requires_confirmation: bool = False
    tier: WorkTier = WorkTier.PROCEDURAL
    pure: bool = False
    replay_safe: bool = False
    has_selection_grounding: bool = False
    metrics: RouteMetrics = field(default_factory=lambda: RouteMetrics(0, 0, 0))

    @property
    def digest(self) -> str:
        payload = {
            "procedure_id": self.procedure_id,
            "version": self.version,
            "intent": self.intent,
            "arguments": [
                {
                    "name": argument.name,
                    "value_type": argument.value_type.value,
                    "min_length": argument.min_length,
                    "max_length": argument.max_length,
                    "allow_blank": argument.allow_blank,
                }
                for argument in self.arguments
            ],
            "effect": self.effect.value,
            "risk": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
            "tier": int(self.tier),
            "pure": self.pure,
            "replay_safe": self.replay_safe,
            "has_selection_grounding": self.has_selection_grounding,
            "metrics": {
                "cost": self.metrics.cost,
                "latency": self.metrics.latency,
                "intelligence": self.metrics.intelligence,
            },
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()


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


def normalize_prompt(prompt: object) -> str:
    if type(prompt) is not str:
        raise InvalidPrompt("prompt must be a string")
    if len(prompt) > PROMPT_MAX_LENGTH:
        raise InvalidPrompt(f"prompt must be at most {PROMPT_MAX_LENGTH} characters")
    normalized = prompt.strip()
    if not normalized:
        raise InvalidPrompt()
    return normalized


def parse_probability(value: object) -> float | None:
    if type(value) is bool or not isinstance(value, (int, float)):
        return None
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not isfinite(normalized) or normalized < 0.0 or normalized > 1.0:
        return None
    return normalized


def is_valid_selector_provider_id(value: object) -> bool:
    return isinstance(value, str) and _SELECTOR_PROVIDER_ID_PATTERN.fullmatch(value) is not None


def is_valid_classifier_id(value: object) -> bool:
    return isinstance(value, str) and _CLASSIFIER_ID_PATTERN.fullmatch(value) is not None


def is_valid_procedure_id(value: object) -> bool:
    return isinstance(value, str) and _PROCEDURE_ID_PATTERN.fullmatch(value) is not None


def is_valid_procedure_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= PROCEDURE_VERSION_MAX_LENGTH
        and _PROCEDURE_VERSION_PATTERN.fullmatch(value) is not None
    )


def is_valid_procedure_intent(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= PROCEDURE_INTENT_MAX_LENGTH
        and _PROCEDURE_INTENT_PATTERN.fullmatch(value) is not None
    )


def is_valid_argument_name(value: object) -> bool:
    return isinstance(value, str) and _ARGUMENT_NAME_PATTERN.fullmatch(value) is not None
