"""Provider-neutral statistical judgments; policy and execution remain ordinary code.

Probability is evidence for a particular answer. Provider confidence may instead
measure distribution concentration: the two values must never be interchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from math import isclose
from typing import Protocol

from af.framework.types import SelectorLocality, parse_probability

DECISION_STATE_MAX_LENGTH = 32_000
DECISION_QUESTION_MAX_LENGTH = 4_000
DECISION_OPTIONS_MAX = 255
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


class DecisionKind(StrEnum):
    BINARY = "binary"
    CHOICE = "choice"


class DecisionStatus(StrEnum):
    ANSWERED = "answered"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def _text(value: str, limit: int, *, blank: bool = False) -> None:
    if type(value) is not str or len(value) > limit or (not blank and not value.strip()):
        raise ValueError("decision text is invalid")


def _probability(value: float) -> None:
    if parse_probability(value) is None:
        raise ValueError("decision probability must be finite and in [0, 1]")


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    id: str
    description: str

    def __post_init__(self) -> None:
        if type(self.id) is not str or _ID.fullmatch(self.id) is None:
            raise ValueError("decision option ID is invalid")
        _text(self.description, DECISION_QUESTION_MAX_LENGTH)


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    """Immutable bounded text state plus a closed choice set or binary question."""

    kind: DecisionKind
    question: str
    state: str
    options: tuple[ChoiceOption, ...] = ()

    def __post_init__(self) -> None:
        if type(self.kind) is not DecisionKind:
            raise ValueError("decision kind is invalid")
        _text(self.question, DECISION_QUESTION_MAX_LENGTH)
        _text(self.state, DECISION_STATE_MAX_LENGTH, blank=True)
        if type(self.options) is not tuple or any(
            type(item) is not ChoiceOption for item in self.options
        ):
            raise ValueError("decision options are invalid")
        if self.kind is DecisionKind.BINARY and self.options:
            raise ValueError("binary decisions have no options")
        if self.kind is DecisionKind.CHOICE and not 2 <= len(self.options) <= DECISION_OPTIONS_MAX:
            raise ValueError("choice decisions require 2 to 255 options")
        if len({item.id for item in self.options}) != len(self.options):
            raise ValueError("decision option IDs must be unique")
        if len(self.canonical_json().encode("utf-8")) > 128_000:
            raise ValueError("decision request is too large")

    def canonical_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind.value,
                "question": self.question,
                "state": self.state,
                "options": [
                    {"id": item.id, "description": item.description} for item in self.options
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_json().encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Probability:
    option: str
    value: float

    def __post_init__(self) -> None:
        if type(self.option) is not str or _ID.fullmatch(self.option) is None:
            raise ValueError("decision option ID is invalid")
        _probability(self.value)


@dataclass(frozen=True, slots=True)
class DecisionAnswer:
    request_digest: str
    provider: str
    status: DecisionStatus = DecisionStatus.ANSWERED
    model: str | None = None
    probability: float | None = None
    probabilities: tuple[Probability, ...] = ()
    provider_confidence: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if (
            type(self.request_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.request_digest) is None
        ):
            raise ValueError("decision request digest is invalid")
        if type(self.provider) is not str or _ID.fullmatch(self.provider) is None:
            raise ValueError("decision provider is invalid")
        if type(self.status) is not DecisionStatus:
            raise ValueError("decision status is invalid")
        if self.model is not None:
            _text(self.model, 128)
        if self.probability is not None:
            _probability(self.probability)
        if self.provider_confidence is not None:
            _probability(self.provider_confidence)
        if (
            type(self.probabilities) is not tuple
            or len(self.probabilities) > DECISION_OPTIONS_MAX
            or any(type(item) is not Probability for item in self.probabilities)
        ):
            raise ValueError("decision probabilities are invalid")
        if any(
            type(value) is not int or not 0 <= value <= 10_000_000
            for value in (self.input_tokens, self.output_tokens)
        ):
            raise ValueError("decision usage is invalid")
        if self.status is not DecisionStatus.ANSWERED and (
            self.probability is not None
            or self.probabilities
            or self.provider_confidence is not None
        ):
            raise ValueError("failed decisions cannot contain answers")


class DecisionProvider(Protocol):
    name: str
    locality: SelectorLocality

    def evaluate(
        self, request: DecisionRequest, *, cancelled: Callable[[], bool] | None = None
    ) -> DecisionAnswer: ...


def reduce_decision(
    request: DecisionRequest,
    answer: DecisionAnswer,
    *,
    threshold: float = 0.8,
    minimum_margin: float = 0.0,
) -> bool | str | None:
    """Apply explicit deterministic policy; None preserves ambiguity/failure.

    The caller still owns authorization, feasibility, independent verification,
    and execution. A high probability is never execution permission.
    """
    _probability(threshold)
    _probability(minimum_margin)
    if request.kind is DecisionKind.BINARY and threshold <= 0.5:
        raise ValueError("binary threshold must exceed one half")
    if answer.status is not DecisionStatus.ANSWERED or answer.request_digest != request.digest:
        return None
    if request.kind is DecisionKind.BINARY:
        if answer.probability is None or answer.probabilities:
            return None
        if answer.probability >= threshold:
            return True
        if answer.probability <= 1.0 - threshold:
            return False
        return None
    if answer.probability is not None:
        return None
    distribution = {item.option: item.value for item in answer.probabilities}
    if len(distribution) != len(answer.probabilities) or set(distribution) != {
        option.id for option in request.options
    }:
        return None
    if not isclose(sum(distribution.values()), 1.0, abs_tol=1e-6):
        return None
    ordered = sorted(distribution, key=lambda option: (-distribution[option], option))
    winner, runner_up = ordered[:2]
    if (
        distribution[winner] < threshold
        or distribution[winner] <= distribution[runner_up]
        or distribution[winner] - distribution[runner_up] < minimum_margin
    ):
        return None
    return winner


class DeterministicDecisionProvider:
    """An injected trusted rule supplies judgments without any model dependency."""

    name = "deterministic"
    locality = SelectorLocality.LOCAL

    def __init__(self, rule: Callable[[DecisionRequest], DecisionAnswer]) -> None:
        self._rule = rule

    def evaluate(
        self, request: DecisionRequest, *, cancelled: Callable[[], bool] | None = None
    ) -> DecisionAnswer:
        if cancelled is not None and cancelled():
            return DecisionAnswer(request.digest, self.name, DecisionStatus.CANCELLED)
        return self._rule(request)
