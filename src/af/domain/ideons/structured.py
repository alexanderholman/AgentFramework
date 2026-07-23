"""Structured inquiry results and transport records.

The original :mod:`af.domain.ideons.records` values remain the stable flat
compatibility contract.  This module adds the authoritative recursive value
carried by the deterministic Prime Language runtime without introducing a
cycle from ``records`` back into ``questions``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .questions import QExpression, QLeaf, q_expression_record, validate_q_expression
from .records import InquiryResult, InterpretationRecord


@dataclass(frozen=True, slots=True)
class StructuredInquiryResult(InquiryResult):
    """An inquiry result with one authoritative recursive expression.

    The inherited scalar fields remain available for compatibility and fast
    routing.  ``expression`` is the lossless value: its question and closure
    status must agree with the compatibility projection.
    """

    expression: QExpression | None = None

    def __post_init__(self) -> None:
        InquiryResult.__post_init__(self)
        expression = self.expression
        if expression is None:
            expression = QLeaf(
                question=self.question,
                value_kind="compatibility-projection",
                value_ref=self.required_information or self.role or self.status.value,
                status=self.status,
                role=self.role,
                evidence=self.evidence,
            )
            object.__setattr__(self, "expression", expression)
        validate_q_expression(expression)
        if expression.question is not self.question:
            raise ValueError("inquiry expression question must match its result")
        if expression.status is not self.status:
            raise ValueError("inquiry expression status must match its result")


class StructuredInterpretationRecord(InterpretationRecord):
    """Interpretation record whose question transport includes expressions."""

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        questions = payload["questions"]
        if not isinstance(questions, dict):
            raise TypeError("questions transport must be a mapping")
        for result in self.inquiries.results:
            if not isinstance(result, StructuredInquiryResult):
                raise TypeError("structured record requires structured inquiry results")
            question_payload = questions[result.question.value]
            if not isinstance(question_payload, dict):
                raise TypeError("question transport entry must be a mapping")
            if result.expression is None:  # guarded by StructuredInquiryResult
                raise TypeError("structured inquiry expression must not be absent")
            question_payload["expression"] = q_expression_record(result.expression)
        return payload


def structured_record(record: InterpretationRecord) -> StructuredInterpretationRecord:
    """Copy a compatible record into the structured transport subtype."""

    return StructuredInterpretationRecord(
        source_id=record.source_id,
        source_span=record.source_span,
        rights=record.rights,
        forms=record.forms,
        candidate_sets=record.candidate_sets,
        selected_entry_ids=record.selected_entry_ids,
        ideon_key=record.ideon_key,
        ideon_revision=record.ideon_revision,
        context=record.context,
        inquiries=record.inquiries,
        openings=record.openings,
        decision=record.decision,
        actual_output=record.actual_output,
        should_assessment=record.should_assessment,
        schema_version="ideon-record-v0.2",
    )
