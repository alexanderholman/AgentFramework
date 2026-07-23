"""Recursive inquiry expressions shared by every Prime Language question type.

Recursion is a property of the inquiry/value structure generally. ``WHEN`` is not
special because it can contain a collection: WHO, WHAT, WHERE, WHEN, WHERN, WHY,
HOW, IS, COULD, WOULD, and SHOULD may all be represented as bounded leaves or
nested groups. Specialist modules remain responsible for validating the meaning
of their leaf values.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .records import InquiryStatus, Question

MAX_Q_DEPTH = 32
MAX_Q_NODES = 1_024
MAX_Q_MEMBERS = 64
MAX_Q_TEXT = 2_048


class QOperator(StrEnum):
    """Finite composition operators for recursive inquiry expressions."""

    ALL = "all"
    ANY = "any"
    SEQUENCE = "sequence"
    TRIGGER = "trigger"


def _bounded_text(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if not value:
        raise ValueError(f"{label} must not be empty")
    if len(value) > MAX_Q_TEXT:
        raise ValueError(f"{label} exceeds the {MAX_Q_TEXT}-character limit")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")


@dataclass(frozen=True, slots=True)
class QLeaf:
    """One specialist-validated value or opening for a question type.

    ``value_kind`` identifies the specialist value schema and ``value_ref`` points to
    its authoritative immutable value or record. The generic recursive contract does
    not reinterpret that value. For example, a WHEN leaf may refer to a timestamp,
    event predicate, criterion, interval, ordering, or situated atomic-clock mark.
    """

    question: Question
    value_kind: str
    value_ref: str
    status: InquiryStatus = InquiryStatus.SATISFIED
    role: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.question, Question):
            raise TypeError("question must be a Question")
        if not isinstance(self.status, InquiryStatus):
            raise TypeError("status must be an InquiryStatus")
        _bounded_text(self.value_kind, "value kind")
        _bounded_text(self.value_ref, "value reference")
        if self.role is not None:
            _bounded_text(self.role, "role")
        for item in self.evidence:
            _bounded_text(item, "evidence")


@dataclass(frozen=True, slots=True)
class QGroup:
    """A bounded recursive composition of inquiry expressions.

    Child questions may differ from the group question. This is required for
    compositions such as WHERN, whose children are a WHERE expression and a WHEN
    expression. The group status is explicit rather than inferred generically because
    closure rules are specialist- and operator-dependent.
    """

    question: Question
    operator: QOperator
    members: tuple[QLeaf | QGroup, ...]
    status: InquiryStatus
    role: str | None = None
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.question, Question):
            raise TypeError("question must be a Question")
        if not isinstance(self.operator, QOperator):
            raise TypeError("operator must be a QOperator")
        if not isinstance(self.status, InquiryStatus):
            raise TypeError("status must be an InquiryStatus")
        if not self.members:
            raise ValueError("question group must contain at least one member")
        if len(self.members) > MAX_Q_MEMBERS:
            raise ValueError(f"question group exceeds the {MAX_Q_MEMBERS}-member limit")
        if self.role is not None:
            _bounded_text(self.role, "role")
        for item in self.evidence:
            _bounded_text(item, "evidence")
        validate_q_expression(self)


type QExpression = QLeaf | QGroup


def expression_depth(expression: QExpression) -> int:
    """Return the maximum node depth, counting one leaf as depth one."""

    if isinstance(expression, QLeaf):
        return 1
    return 1 + max(expression_depth(member) for member in expression.members)


def expression_node_count(expression: QExpression) -> int:
    """Return the total number of leaves and groups in an expression."""

    if isinstance(expression, QLeaf):
        return 1
    return 1 + sum(expression_node_count(member) for member in expression.members)


def validate_q_expression(expression: QExpression) -> None:
    """Enforce global recursion bounds without applying specialist semantics."""

    depth = expression_depth(expression)
    if depth > MAX_Q_DEPTH:
        raise ValueError(f"question expression exceeds the maximum depth of {MAX_Q_DEPTH}")
    nodes = expression_node_count(expression)
    if nodes > MAX_Q_NODES:
        raise ValueError(
            f"question expression exceeds the maximum size of {MAX_Q_NODES} nodes"
        )


def q_expression_record(expression: QExpression) -> dict[str, Any]:
    """Return a deterministic JSON-compatible record for identity and transport."""

    if isinstance(expression, QLeaf):
        return {
            "evidence": list(expression.evidence),
            "kind": "leaf",
            "question": expression.question.value,
            "role": expression.role,
            "status": expression.status.value,
            "value_kind": expression.value_kind,
            "value_ref": expression.value_ref,
        }
    return {
        "evidence": list(expression.evidence),
        "kind": "group",
        "members": [q_expression_record(member) for member in expression.members],
        "operator": expression.operator.value,
        "question": expression.question.value,
        "role": expression.role,
        "status": expression.status.value,
    }


def q_expression_bytes(expression: QExpression) -> bytes:
    """Return canonical UTF-8 bytes for a validated expression."""

    validate_q_expression(expression)
    return json.dumps(
        q_expression_record(expression),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def q_expression_id(expression: QExpression) -> str:
    """Return the immutable SHA-256 identity of the canonical expression record."""

    return hashlib.sha256(q_expression_bytes(expression)).hexdigest()
