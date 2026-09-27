"""Built-in procedures; these do work while the agent coordinates it."""

from __future__ import annotations

import datetime as dt
import re
from abc import ABC, abstractmethod
from zoneinfo import ZoneInfo

from af.framework.errors import InvalidCharacter, InvalidSelectionProposal
from af.framework.types import (
    PROMPT_MAX_LENGTH,
    ArgumentValueType,
    Classification,
    ProcedureArgumentDescriptor,
    ProcedureDescriptor,
    ProcedureEffect,
    ProcedureResult,
    ProcedureRisk,
    RouteMetrics,
    WorkRequest,
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
    has_selection_grounding = False
    metrics: RouteMetrics

    @abstractmethod
    def supports(self, classification: Classification) -> bool:
        raise NotImplementedError

    @abstractmethod
    def execute(self, classification: Classification) -> ProcedureResult:
        raise NotImplementedError

    @abstractmethod
    def validate(self, classification: Classification) -> None:
        raise NotImplementedError

    def selection_is_grounded(
        self,
        request: WorkRequest,
        classification: Classification,
    ) -> bool:
        del request, classification
        return False


class PureProcedureMixin:
    """Python's trait-equivalent for side-effect-free, replay-safe procedures."""

    pure = True
    replay_safe = True


class HelloProcedure(PureProcedureMixin, BaseProcedure):
    id = "hello"
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=BaseProcedure.version,
        intent="system.hello",
        arguments=(),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
        requires_confirmation=False,
    )
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

    def validate(self, classification: Classification) -> None:
        if classification.intent != "system.hello" or classification.arguments:
            raise InvalidSelectionProposal("unsupported intent or arguments for hello")


class CharacterCountProcedure(PureProcedureMixin, BaseProcedure):
    _selection_pattern = re.compile(
        r"^\s*(?:please\s+)?count\s+(?:the\s+letter\s+)?"
        r"(?P<character>[^\W\d_])\s+in\s+(?P<text>.+?)\s*[?.!]*\s*$",
        re.IGNORECASE,
    )
    id = "character-count"
    has_selection_grounding = True
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=BaseProcedure.version,
        intent="text.character_count",
        arguments=(
            ProcedureArgumentDescriptor(
                name="text",
                value_type=ArgumentValueType.TEXT,
                min_length=1,
                max_length=PROMPT_MAX_LENGTH,
                allow_blank=True,
            ),
            ProcedureArgumentDescriptor(
                name="character",
                value_type=ArgumentValueType.TEXT,
                min_length=1,
                max_length=1,
                allow_blank=True,
            ),
        ),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
        requires_confirmation=False,
    )
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

    def validate(self, classification: Classification) -> None:
        if classification.intent != "text.character_count":
            raise InvalidSelectionProposal("unsupported intent for character-count")
        if set(classification.arguments) != {"text", "character"}:
            raise InvalidSelectionProposal("invalid arguments for character-count")
        text = classification.arguments["text"]
        character = classification.arguments["character"]
        if not text or len(character) != 1:
            raise InvalidSelectionProposal("invalid arguments for character-count")

    def selection_is_grounded(
        self,
        request: WorkRequest,
        classification: Classification,
    ) -> bool:
        match = self._selection_pattern.fullmatch(request.prompt)
        if match is None:
            return False
        return classification.arguments == {
            "text": match.group("text").strip(),
            "character": match.group("character").casefold(),
        }


class CurrentTimeProcedure(BaseProcedure):
    id = "current-time"
    version = "1.1.0"
    selection_descriptor = ProcedureDescriptor(
        procedure_id=id,
        version=version,
        intent="system.current_time",
        arguments=(
            ProcedureArgumentDescriptor(
                name="timezone",
                value_type=ArgumentValueType.TEXT,
                min_length=1,
                max_length=64,
                allow_blank=False,
            ),
        ),
        effect=ProcedureEffect.READ_ONLY,
        risk=ProcedureRisk.NONE,
        requires_confirmation=False,
    )
    tier = WorkTier.STATIC
    metrics = RouteMetrics(cost=0, latency=0, intelligence=0)

    def supports(self, classification: Classification) -> bool:
        return classification.intent == "system.current_time"

    def execute(self, classification: Classification) -> ProcedureResult:
        self.validate(classification)
        tz_name = classification.arguments.get("timezone", "UTC")
        now = dt.datetime.now(ZoneInfo(tz_name))
        iso = now.isoformat()
        return ProcedureResult(
            output=iso,
            evidence={"timezone": tz_name, "iso": iso},
        )

    def validate(self, classification: Classification) -> None:
        if classification.intent != "system.current_time":
            raise InvalidSelectionProposal("unsupported intent for current-time")
        allowed = {"timezone"}
        if not set(classification.arguments.keys()).issubset(allowed):
            raise InvalidSelectionProposal("invalid arguments for current-time")
        zone = classification.arguments.get("timezone", "UTC")
        if type(zone) is not str or not 1 <= len(zone) <= 64:
            raise InvalidSelectionProposal("invalid IANA timezone for current-time")
        try:
            ZoneInfo(zone)
        except (KeyError, ValueError):
            raise InvalidSelectionProposal("invalid IANA timezone for current-time") from None
