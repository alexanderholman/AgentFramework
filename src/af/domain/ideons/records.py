"""Typed records shared by deterministic and future neural Ideon routes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from .numeric import natural_to_decimal

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}$")


def _identifier(value: str, label: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a bounded identifier")


def _text(value: str, label: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise ValueError(f"{label} must be text")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")


class Question(StrEnum):
    WHO = "who"
    WHAT = "what"
    WHERE = "where"
    WHEN = "when"
    WHERN = "whern"
    WHY = "why"
    HOW = "how"
    IS = "is"
    COULD = "could"
    WOULD = "would"
    SHOULD = "should"


QUESTIONS: tuple[Question, ...] = tuple(Question)


class InquiryStatus(StrEnum):
    SATISFIED = "satisfied"
    OPEN = "open"
    CONTESTED = "contested"
    NOT_APPLICABLE = "not-applicable"


class Decision(StrEnum):
    ASK = "ask"
    ANSWER = "answer"
    ACT = "act"
    STOP = "stop"
    CONTINUE_READING = "continue-reading"


class CandidateStatus(StrEnum):
    VIABLE = "viable"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class LanguageProfileRef:
    language: str
    version: str
    unicode_version: str
    normalization: Literal["NFC", "NFD", "NFKC", "NFKD"] = "NFC"
    dialect: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.language, "language")
        _identifier(self.version, "language profile version")
        _identifier(self.unicode_version, "Unicode version")
        _identifier(self.normalization, "normalization")
        if self.dialect is not None:
            _identifier(self.dialect, "dialect")

    @property
    def identifier(self) -> str:
        suffix = f"-{self.dialect}" if self.dialect else ""
        return (
            f"{self.language}{suffix}@{self.version}"
            f"/unicode-{self.unicode_version}/normalization-{self.normalization}"
        )


@dataclass(frozen=True, slots=True)
class Form:
    surface: str
    normalized: str
    form_type: str
    codepoint_span: tuple[int, int]
    byte_span: tuple[int, int]

    def __post_init__(self) -> None:
        _text(self.surface, "surface form")
        _text(self.normalized, "normalized form")
        _identifier(self.form_type, "form type")
        for label, span in (
            ("codepoint span", self.codepoint_span),
            ("byte span", self.byte_span),
        ):
            if len(span) != 2 or span[0] < 0 or span[1] <= span[0]:
                raise ValueError(f"{label} must be an increasing non-negative pair")


@dataclass(frozen=True, slots=True)
class FormSequence:
    surface: str
    original_utf8: bytes
    profile: LanguageProfileRef
    forms: tuple[Form, ...]

    def __post_init__(self) -> None:
        _text(self.surface, "surface", allow_empty=True)
        if self.original_utf8 != self.surface.encode("utf-8"):
            raise ValueError("original UTF-8 bytes must losslessly encode surface")
        previous_codepoint = 0
        previous_byte = 0
        for form in self.forms:
            codepoint_start, codepoint_end = form.codepoint_span
            byte_start, byte_end = form.byte_span
            if codepoint_start < previous_codepoint or byte_start < previous_byte:
                raise ValueError("forms must be in source order without overlapping")
            if self.surface[codepoint_start:codepoint_end] != form.surface:
                raise ValueError("form codepoint span does not match source")
            if self.original_utf8[byte_start:byte_end] != form.surface.encode("utf-8"):
                raise ValueError("form byte span does not match source")
            previous_codepoint = codepoint_end
            previous_byte = byte_end


@dataclass(frozen=True, slots=True)
class ContextGraph:
    """Small immutable context projection with explicit identity-bearing fields."""

    values: tuple[tuple[str, str], ...] = ()
    identity_fields: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        names: set[str] = set()
        for name, value in self.values:
            _identifier(name, "context field")
            _text(value, "context value")
            if name in names:
                raise ValueError(f"duplicate context field: {name}")
            names.add(name)
        if tuple(sorted(self.values)) != self.values:
            raise ValueError("context values must be sorted by field")
        if len(set(self.identity_fields)) != len(self.identity_fields):
            raise ValueError("identity fields must be unique")
        if tuple(sorted(self.identity_fields)) != self.identity_fields:
            raise ValueError("identity fields must be sorted")
        if not set(self.identity_fields).issubset(names):
            raise ValueError("identity fields must name present context values")

    @classmethod
    def from_dict(
        cls,
        values: dict[str, str] | None = None,
        *,
        identity_fields: tuple[str, ...] = (),
    ) -> ContextGraph:
        return cls(tuple(sorted((values or {}).items())), tuple(sorted(identity_fields)))

    def get(self, name: str) -> str | None:
        return dict(self.values).get(name)

    @property
    def identity_values(self) -> tuple[tuple[str, str], ...]:
        identity = set(self.identity_fields)
        return tuple(item for item in self.values if item[0] in identity)


@dataclass(frozen=True, slots=True)
class SenseEntry:
    entry_id: str
    ideon_key: int
    ideon_revision: str
    profile_id: str
    forms: tuple[str, ...]
    lexeme: str
    sense: str
    semantic_type: str
    question_slot: Question
    valid_contexts: tuple[tuple[str, str], ...] = ()
    excluded_contexts: tuple[tuple[str, str], ...] = ()
    provenance: tuple[str, ...] = ()
    evidence_status: str = "defined"
    rights: str = "internal-test-fixture"

    def __post_init__(self) -> None:
        _identifier(self.entry_id, "entry id")
        if isinstance(self.ideon_key, bool) or self.ideon_key <= 0:
            raise ValueError("sense Ideon key must be a positive natural number")
        if not re.fullmatch(r"[0-9a-f]{64}", self.ideon_revision):
            raise ValueError("sense revision must be a SHA-256 hexadecimal digest")
        _identifier(self.profile_id, "profile id")
        if not self.forms:
            raise ValueError("dictionary entry must contain at least one form")
        for form in self.forms:
            _text(form, "dictionary form")
        _identifier(self.lexeme, "lexeme")
        _identifier(self.sense, "sense")
        _identifier(self.semantic_type, "semantic type")
        if tuple(sorted(self.valid_contexts)) != self.valid_contexts:
            raise ValueError("valid contexts must be sorted")
        if tuple(sorted(self.excluded_contexts)) != self.excluded_contexts:
            raise ValueError("excluded contexts must be sorted")


@dataclass(frozen=True, slots=True)
class SenseCandidate:
    form_index: int
    entry: SenseEntry
    status: CandidateStatus = CandidateStatus.VIABLE
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.form_index < 0:
            raise ValueError("form index must not be negative")


@dataclass(frozen=True, slots=True)
class CandidateSet:
    form_index: int
    surface: str
    candidates: tuple[SenseCandidate, ...]

    @property
    def viable(self) -> tuple[SenseCandidate, ...]:
        return tuple(
            candidate for candidate in self.candidates if candidate.status is CandidateStatus.VIABLE
        )


@dataclass(frozen=True, slots=True)
class InquiryResult:
    question: Question
    status: InquiryStatus
    role: str | None = None
    candidate_keys: tuple[int, ...] = ()
    required_information: str | None = None
    material: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.role is not None:
            _identifier(self.role, "inquiry role")
        if self.required_information is not None:
            _text(self.required_information, "required information")
        if any(isinstance(key, bool) or key <= 0 for key in self.candidate_keys):
            raise ValueError("candidate keys must be positive natural numbers")
        if self.material and self.status not in {
            InquiryStatus.OPEN,
            InquiryStatus.CONTESTED,
        }:
            raise ValueError("only open or contested inquiries can be material")


@dataclass(frozen=True, slots=True)
class InquiryState:
    results: tuple[InquiryResult, ...]

    def __post_init__(self) -> None:
        if tuple(result.question for result in self.results) != QUESTIONS:
            raise ValueError("inquiry state must contain the eleven questions in canonical order")

    def get(self, question: Question) -> InquiryResult:
        return self.results[QUESTIONS.index(question)]

    @property
    def material_openings(self) -> tuple[InquiryResult, ...]:
        return tuple(result for result in self.results if result.material)


@dataclass(frozen=True, slots=True)
class ShouldAssessment:
    action: str
    affected_entities: tuple[str, ...]
    norm_or_goal: str
    evidence: tuple[str, ...]
    relevance: Decimal
    severity: Decimal
    confidence_or_margin: Decimal
    reversibility: Decimal
    context: ContextGraph
    recommended_alternative: str | None = None
    forbidden: bool = False

    def __post_init__(self) -> None:
        _text(self.action, "action")
        _text(self.norm_or_goal, "norm or goal")
        for label, value in (
            ("relevance", self.relevance),
            ("severity", self.severity),
            ("confidence", self.confidence_or_margin),
            ("reversibility", self.reversibility),
        ):
            if value < Decimal(0) or value > Decimal(1):
                raise ValueError(f"{label} must be within [0, 1]")
        if self.recommended_alternative is not None:
            _text(self.recommended_alternative, "recommended alternative")


@dataclass(frozen=True, slots=True)
class DecisionTrace:
    decision: Decision
    rule_ids: tuple[str, ...]
    evidence: tuple[str, ...] = ()
    question: Question | None = None
    safer_alternative: str | None = None
    policy: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if tuple(sorted(self.policy)) != self.policy:
            raise ValueError("decision policy fields must be sorted")
        if len({name for name, _ in self.policy}) != len(self.policy):
            raise ValueError("decision policy fields must be unique")
        for name, value in self.policy:
            _identifier(name, "decision policy field")
            _text(value, "decision policy value")


@dataclass(frozen=True, slots=True)
class InterpretationRecord:
    """Canonical runtime/training record with operational metadata deliberately omitted."""

    source_id: str
    source_span: tuple[int, int]
    rights: str
    forms: FormSequence
    candidate_sets: tuple[CandidateSet, ...]
    selected_entry_ids: tuple[str, ...]
    ideon_key: int
    ideon_revision: str
    context: ContextGraph
    inquiries: InquiryState
    openings: tuple[str, ...]
    decision: DecisionTrace
    actual_output: tuple[tuple[str, str], ...]
    should_assessment: ShouldAssessment | None = None
    schema_version: str = "ideon-record-v0.1"

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source id")
        if self.source_span[0] < 0 or self.source_span[1] < self.source_span[0]:
            raise ValueError("source span must be ordered and non-negative")
        if isinstance(self.ideon_key, bool) or self.ideon_key <= 0:
            raise ValueError("Ideon key must be a positive natural number")
        if not re.fullmatch(r"[0-9a-f]{64}", self.ideon_revision):
            raise ValueError("Ideon revision must be a SHA-256 hexadecimal digest")

    @property
    def record_id(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        return {
            "actual_output": dict(self.actual_output),
            "context_graph": {
                "identity_fields": list(self.context.identity_fields),
                "values": dict(self.context.values),
            },
            "decision": {
                "decision": self.decision.decision.value,
                "evidence": list(self.decision.evidence),
                "policy": dict(self.decision.policy),
                "question": self.decision.question.value if self.decision.question else None,
                "rule_ids": list(self.decision.rule_ids),
                "safer_alternative": self.decision.safer_alternative,
            },
            "forms": {
                "items": [
                    {
                        "byte_span": list(form.byte_span),
                        "codepoint_span": list(form.codepoint_span),
                        "form_type": form.form_type,
                        "normalized": form.normalized,
                        "surface": form.surface,
                    }
                    for form in self.forms.forms
                ],
                "original_utf8_hex": self.forms.original_utf8.hex(),
                "profile": self.forms.profile.identifier,
                "surface": self.forms.surface,
            },
            "ideon_key": natural_to_decimal(self.ideon_key),
            "ideon_revision": self.ideon_revision,
            "lexeme_candidates": [
                {
                    "candidates": [
                        {
                            "entry_id": candidate.entry.entry_id,
                            "ideon_key": natural_to_decimal(candidate.entry.ideon_key),
                            "reasons": list(candidate.reasons),
                            "sense": candidate.entry.sense,
                            "status": candidate.status.value,
                        }
                        for candidate in candidate_set.candidates
                    ],
                    "form_index": candidate_set.form_index,
                    "surface": candidate_set.surface,
                }
                for candidate_set in self.candidate_sets
            ],
            "openings": list(self.openings),
            "questions": {
                result.question.value: {
                    "candidate_keys": [natural_to_decimal(key) for key in result.candidate_keys],
                    "evidence": list(result.evidence),
                    "material": result.material,
                    "required_information": result.required_information,
                    "role": result.role,
                    "status": result.status.value,
                }
                for result in self.inquiries.results
            },
            "rights": self.rights,
            "schema_version": self.schema_version,
            "selected_senses": list(self.selected_entry_ids),
            "should_assessment": self._should_dict(),
            "source": {"id": self.source_id, "span": list(self.source_span)},
        }

    def _should_dict(self) -> dict[str, Any] | None:
        assessment = self.should_assessment
        if assessment is None:
            return None
        return {
            "action": assessment.action,
            "affected_entities": list(assessment.affected_entities),
            "norm_or_goal": assessment.norm_or_goal,
            "evidence": list(assessment.evidence),
            "relevance": str(assessment.relevance),
            "severity": str(assessment.severity),
            "confidence_or_margin": str(assessment.confidence_or_margin),
            "reversibility": str(assessment.reversibility),
            "context_graph": {
                "identity_fields": list(assessment.context.identity_fields),
                "values": dict(assessment.context.values),
            },
            "recommended_alternative": assessment.recommended_alternative,
            "forbidden": assessment.forbidden,
        }
