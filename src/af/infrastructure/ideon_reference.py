"""Composition helpers for the controlled Principia v0 Ideon reference slice."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from af.application.ideons import IdeonController, IdeonService
from af.domain.ideons import IdeonGraph, IdeonIdentity, IdeonKind, IdeonRef
from af.domain.ideons.dictionary import LogicDictionary
from af.domain.ideons.records import LanguageProfileRef, Question, SenseEntry
from af.infrastructure.ideons import InMemoryIdeonRegistry

REFERENCE_IDEON_PROFILE = LanguageProfileRef(
    language="en",
    dialect="GB",
    version="0.1",
    unicode_version=unicodedata.unidata_version,
)


@dataclass(frozen=True, slots=True)
class ReferenceIdeonRuntime:
    controller: IdeonController
    service: IdeonService
    store: InMemoryIdeonRegistry


@dataclass(frozen=True, slots=True)
class _SeedSense:
    entry_id: str
    key: int
    forms: tuple[str, ...]
    semantic_type: str
    question: Question
    valid_contexts: tuple[tuple[str, str], ...] = ()
    semantic_roles: tuple[str, ...] = ()


_SEED_SENSES = (
    _SeedSense("sit.past", 3, ("sat",), "action", Question.WHAT, (), ("agent",)),
    _SeedSense("on.spatial", 5, ("on",), "relation", Question.WHERE, (), ("location",)),
    _SeedSense("the.definite", 7, ("the",), "property", Question.WHAT),
    _SeedSense(
        "bank.river-edge",
        11,
        ("bank",),
        "entity",
        Question.WHERE,
        (("domain", "river"),),
    ),
    _SeedSense(
        "bank.financial-institution",
        13,
        ("bank",),
        "entity",
        Question.WHERE,
        (("domain", "finance"),),
    ),
    _SeedSense(
        "dictionary.reference-work",
        17,
        ("dictionary",),
        "entity",
        Question.WHAT,
    ),
    _SeedSense("second.ordinal", 19, ("2nd",), "property", Question.WHAT),
    _SeedSense(
        "edition.publication-instance",
        23,
        ("edition",),
        "entity",
        Question.WHAT,
    ),
    _SeedSense(
        "will.future-auxiliary",
        29,
        ("will",),
        "relation",
        Question.WHEN,
        (("mode", "temporal"),),
    ),
    _SeedSense(
        "will.intends",
        31,
        ("will",),
        "action",
        Question.WHAT,
        (("mode", "volitional"),),
        ("agent",),
    ),
)


def create_reference_ideon_runtime() -> ReferenceIdeonRuntime:
    store = InMemoryIdeonRegistry("prime-language-reference@0.1")
    entries = tuple(_register_seed(store, seed) for seed in _SEED_SENSES)
    service = IdeonService(store, LogicDictionary(entries), REFERENCE_IDEON_PROFILE)
    return ReferenceIdeonRuntime(IdeonController(service), service, store)


def _register_seed(store: InMemoryIdeonRegistry, seed: _SeedSense) -> SenseEntry:
    ref = store.register(
        IdeonGraph(
            identity=IdeonIdentity(
                namespace=f"prime-language/{REFERENCE_IDEON_PROFILE.identifier}",
                ruleset="principia@0.1",
                name=seed.entry_id,
                kind=IdeonKind.PRIME,
            )
        ),
        stable_key=seed.key,
    )
    return _entry_from_ref(seed, ref)


def _entry_from_ref(seed: _SeedSense, ref: IdeonRef) -> SenseEntry:
    return SenseEntry(
        entry_id=seed.entry_id,
        ideon_key=ref.key,
        ideon_revision=ref.revision,
        profile_id=REFERENCE_IDEON_PROFILE.identifier,
        forms=seed.forms,
        lexeme=seed.entry_id.split(".")[0],
        sense=seed.entry_id,
        semantic_type=seed.semantic_type,
        question_slot=seed.question,
        valid_contexts=seed.valid_contexts,
        semantic_roles=seed.semantic_roles,
        provenance=("principia-v0:controlled-fixture",),
        evidence_status="defined-fixture",
        rights="project-authored",
    )
