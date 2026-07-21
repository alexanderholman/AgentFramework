from __future__ import annotations

import pytest

from af.domain.classifiers import CharacterCountClassifier
from af.domain.procedures import CharacterCountProcedure, count_character
from af.framework.errors import InvalidCharacter
from af.framework.types import WorkRequest


def test_count_character_counts_each_occurrence() -> None:
    assert count_character(text="strawberry", character="r") == 3


def test_count_character_is_parameterised() -> None:
    assert count_character(text="blueberry", character="b") == 2


def test_count_character_rejects_more_than_one_character() -> None:
    with pytest.raises(InvalidCharacter):
        count_character(text="strawberry", character="rr")


@pytest.mark.parametrize(
    ("character", "expected"),
    [("s", 1), ("ß", 1)],
)
def test_count_character_matches_caseless_source_code_points_without_expansion(
    character: str, expected: int
) -> None:
    assert count_character(text="Straße", character=character) == expected


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("how many rs in strawberry", {"text": "strawberry", "character": "r"}),
        ("How many r's are in strawberry?", {"text": "strawberry", "character": "r"}),
        ("how many bs in blueberry", {"text": "blueberry", "character": "b"}),
    ],
)
def test_classifier_recognises_constrained_character_questions(
    prompt: str, expected: dict[str, str]
) -> None:
    classification = CharacterCountClassifier().classify(WorkRequest(prompt=prompt))

    assert classification is not None
    assert classification.intent == "text.character_count"
    assert classification.arguments == expected


def test_procedure_explicitly_returns_transport_neutral_output() -> None:
    classification = CharacterCountClassifier().classify(
        WorkRequest(prompt="how many rs in strawberry")
    )
    assert classification is not None

    result = CharacterCountProcedure().execute(classification)

    assert result.output == "3"
    assert result.evidence["count"] == 3
