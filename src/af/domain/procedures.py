"""Built-in procedures; these do work while the agent coordinates it."""

from __future__ import annotations

from abc import ABC, abstractmethod

from af.framework.contracts import ModelProvider
from af.framework.errors import InvalidCharacter
from af.framework.types import (
    Classification,
    ProcedureResult,
    RouteMetrics,
    WorkTier,
)


def count_character(*, text: str, character: str) -> int:
    """Count one character by explicit iteration, independent of model inference."""
    if len(character) != 1:
        raise InvalidCharacter(character)
    count = 0
    folded_character = character.casefold()
    for candidate in text:
        if candidate.casefold() == folded_character:
            count += 1
    return count


class BaseProcedure(ABC):
    """Nominal base for discoverable capabilities; contracts remain structural."""

    id: str
    version = "1.0.0"
    tier: WorkTier
    pure = False
    replay_safe = False
    metrics: RouteMetrics

    @abstractmethod
    def supports(self, classification: Classification) -> bool:
        raise NotImplementedError

    @abstractmethod
    def execute(self, classification: Classification) -> ProcedureResult:
        raise NotImplementedError


class PureProcedureMixin:
    """Python's trait-equivalent for side-effect-free, replay-safe procedures."""

    pure = True
    replay_safe = True


class HelloProcedure(PureProcedureMixin, BaseProcedure):
    id = "hello"
    tier = WorkTier.STATIC
    metrics = RouteMetrics(cost=0, latency=0, intelligence=0)

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "system.hello"

    def execute(self, classification: Classification) -> ProcedureResult:
        del classification
        return ProcedureResult(
            output="Hello, world!",
            evidence={"source": "static"},
        )


class CharacterCountProcedure(PureProcedureMixin, BaseProcedure):
    id = "character-count"
    tier = WorkTier.PROCEDURAL
    metrics = RouteMetrics(cost=1, latency=1, intelligence=1)

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "text.character_count"

    def execute(self, classification: Classification) -> ProcedureResult:
        text = classification.arguments["text"]
        character = classification.arguments["character"]
        count = count_character(text=text, character=character)
        return ProcedureResult(
            output=str(count),
            evidence={"text": text, "character": character, "count": count},
        )


class ModelProcedure(BaseProcedure):
    """Explicit optional generative route; never registered as an implicit fallback."""

    id = "model-provider"
    tier = WorkTier.GENERATIVE
    metrics = RouteMetrics(cost=5, latency=5, intelligence=5)

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "unknown"

    def execute(self, classification: Classification) -> ProcedureResult:
        prompt = classification.arguments.get("prompt", "")
        return ProcedureResult(
            output=self._provider.complete(prompt),
            evidence={"provider": self._provider.name},
        )
