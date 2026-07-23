"""Versioned Principia v0 sets used by the reference language slice."""

from __future__ import annotations

from dataclasses import dataclass

from af.domain.ideons.records import QUESTIONS

LOGIC_SET_V0 = ("NOT", "AND", "OR", "IF", "IFF", "FORALL", "EXISTS", "EQUALS")
TYPE_SET_V0 = (
    "Entity",
    "Collection",
    "Relation",
    "Action",
    "State",
    "Property",
    "Context",
    "Criterion",
    "Proposition",
)
RELATION_SET_V0 = (
    "INSTANCE_OF",
    "SUBTYPE_OF",
    "PART_OF",
    "HAS",
    "DOES",
    "CAUSES",
    "ENABLES",
    "PREVENTS",
    "BEFORE",
    "AFTER",
    "LOCATED_IN",
    "ORIGIN",
    "DESTINATION",
    "REFERS_TO",
    "MEANS",
)
QUESTION_SET_V0 = tuple(question.value.upper() for question in QUESTIONS)


@dataclass(frozen=True, slots=True)
class SetStack:
    prime: str = "principia-prime-v0"
    logic: str = "principia-logic-v0"
    relation: str = "principia-relation-v0"
    language: str = "en-GB-v0"
    definition: str = "fixture-definitions-v0"
    equivalence: str = "strict-equivalence-v0"
    closure: str = "sentence-closure-v0"
    norm: str = "consent-and-harm-v0"
    privacy_security: str = "af-default-v0"

    def as_identity_context(self) -> tuple[tuple[str, str], ...]:
        return (
            ("closure-set", self.closure),
            ("definition-set", self.definition),
            ("equivalence-set", self.equivalence),
            ("language-set", self.language),
            ("logic-set", self.logic),
            ("norm-set", self.norm),
            ("prime-set", self.prime),
            ("privacy-security-set", self.privacy_security),
            ("relation-set", self.relation),
        )
