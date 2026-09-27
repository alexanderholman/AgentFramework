"""Deliberately constrained classifiers for the first compiled AF routes."""

from __future__ import annotations

import re

from af.framework.classification_hierarchy import (
    FunctionRoute,
    HierarchyResult,
    HierarchyRouter,
    NoRoute,
)
from af.framework.contracts import Classifier
from af.framework.types import Classification, WorkRequest


class TimeClassifier:
    """Classify time-related requests with zero confidence (deterministic match)."""

    id = "time-classifier"

    def __init__(self) -> None:
        self._patterns = [
            re.compile(r"\bwhat\s+time\b", re.IGNORECASE),
            re.compile(r"\bwhat\s+is\s+(the\s+)?time\b", re.IGNORECASE),
            re.compile(r"\bwhat[''']s\s+(the\s+)?time\b", re.IGNORECASE),
            re.compile(r"\bcurrent\s+time\b", re.IGNORECASE),
            re.compile(r"\bcurrent\s+datetime\b", re.IGNORECASE),
            re.compile(r"\btime\s+now\b", re.IGNORECASE),
            re.compile(r"\belapsed\s+time\b", re.IGNORECASE),
            re.compile(r"\btime\s+today\b", re.IGNORECASE),
        ]
        self._exclusions = [
            re.compile(r"\btime\s+management\b", re.IGNORECASE),
            re.compile(r"\bwastetime\b", re.IGNORECASE),
            re.compile(r"\bsavetime\b", re.IGNORECASE),
        ]
        self._timezone_pattern = re.compile(
            r"\bin\s+(?:the\s+)?(?:timezone\s+)?(.+?)\s*[?.!]*$", re.IGNORECASE
        )

    def classify(self, request: WorkRequest) -> Classification | None:
        prompt = request.prompt
        if not prompt:
            return None

        for exclusion in self._exclusions:
            if exclusion.search(prompt):
                return None

        for pattern in self._patterns:
            if pattern.search(prompt):
                arguments: dict[str, str] = {"timezone": "UTC"}
                tz_match = self._timezone_pattern.search(prompt)
                if tz_match:
                    arguments["timezone"] = tz_match.group(1)
                return Classification(
                    intent="system.current_time",
                    confidence=1.0,
                    arguments=arguments,
                    classifier=self.id,
                )
        return None


class ClassifierChain:
    def __init__(
        self, classifiers: list[Classifier], *, hierarchy: HierarchyRouter | None = None
    ) -> None:
        self._classifiers = tuple(classifiers)
        self._hierarchy = hierarchy

    def classify_with_trace(self, request: WorkRequest) -> HierarchyResult | None:
        return None if self._hierarchy is None else self._hierarchy.route(request)

    def describe_hierarchy(self) -> dict[str, object]:
        return {"roots": [], "nodes": []} if self._hierarchy is None else self._hierarchy.describe()

    def classify(self, request: WorkRequest) -> Classification | None:
        result = self.classify_with_trace(request)
        if result is not None:
            if isinstance(result.outcome, FunctionRoute):
                proposal = result.outcome.proposal
                return Classification(
                    proposal.intent,
                    dict(proposal.arguments),
                    proposal.confidence,
                    result.visits[-1].node_id,
                )
            if isinstance(result.outcome, NoRoute):
                return Classification("unknown", {}, 0.0, "hierarchy")
            return None
        for classifier in self._classifiers:
            classification = classifier.classify(request)
            if classification is not None:
                return classification
        return None


class HelloClassifier:
    _pattern = re.compile(r"^\s*hello(?:\s+world)?\s*[!.]?\s*$", re.IGNORECASE)

    def classify(self, request: WorkRequest) -> Classification | None:
        if not self._pattern.fullmatch(request.prompt):
            return None
        return Classification(
            intent="system.hello",
            arguments={},
            confidence=1.0,
            classifier=type(self).__name__,
        )


class CharacterCountClassifier:
    _pattern = re.compile(
        r"^\s*how\s+many\s+"
        r"(?P<character>[^\W\d_])(?:'s|s)\s+"
        r"(?:(?:are\s+there|are|occur)\s+)?"
        r"in\s+(?P<text>.+?)\s*\??\s*$",
        re.IGNORECASE,
    )

    def classify(self, request: WorkRequest) -> Classification | None:
        match = self._pattern.fullmatch(request.prompt)
        if match is None:
            return None
        text = match.group("text").removesuffix("?").strip()
        return Classification(
            intent="text.character_count",
            arguments={
                "text": text,
                "character": match.group("character").casefold(),
            },
            confidence=1.0,
            classifier=type(self).__name__,
        )


class UnknownClassifier:
    def classify(self, request: WorkRequest) -> Classification:
        return Classification(
            intent="unknown",
            arguments={"prompt": request.prompt},
            confidence=0.0,
            classifier=type(self).__name__,
        )
