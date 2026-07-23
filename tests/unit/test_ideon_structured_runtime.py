from __future__ import annotations

from af.application.ideons import InterpretRequest
from af.application.structured_ideons import structure_inquiries
from af.domain.ideons.models import IdeonRef
from af.domain.ideons.questions import QGroup, QLeaf, QOperator
from af.domain.ideons.records import ContextGraph, InquiryStatus, Question
from af.domain.ideons.structured import StructuredInquiryResult
from af.infrastructure.ideon_reference import create_reference_ideon_runtime


def _request(context: dict[str, str]) -> InterpretRequest:
    return InterpretRequest(
        surface="sat on the bank",
        source_id="structured:test",
        rights="project-authored",
        context=ContextGraph.from_dict(context),
    )


def test_reference_runtime_carries_recursive_questions_through_transport_and_graph() -> None:
    runtime = create_reference_ideon_runtime()
    record = runtime.controller.interpret(
        _request(
            {
                "actor": "alice",
                "domain": "river",
                "time": "2026-07-23T10:00:00+00:00",
                "when": "event:door-opens",
                "where": "river-bank",
            }
        )
    )

    when = record.inquiries.get(Question.WHEN)
    assert isinstance(when, StructuredInquiryResult)
    assert isinstance(when.expression, QGroup)
    assert when.expression.operator is QOperator.ALL
    assert [
        member.value_kind for member in when.expression.members if isinstance(member, QLeaf)
    ] == [
        "timestamp",
        "event",
    ]

    whern = record.inquiries.get(Question.WHERN)
    assert isinstance(whern, StructuredInquiryResult)
    assert isinstance(whern.expression, QGroup)
    assert tuple(member.question for member in whern.expression.members) == (
        Question.WHERE,
        Question.WHEN,
    )

    payload = record.to_dict()
    assert payload["schema_version"] == "ideon-record-v0.2"
    assert payload["questions"]["when"]["expression"]["operator"] == "all"
    assert len(payload["questions"]["whern"]["expression"]["members"]) == 2

    graph = runtime.service.resolve(IdeonRef(record.ideon_key, record.ideon_revision))
    node_ids = {node.node_id for node in graph.nodes}
    assert "inquiry-when" in node_ids
    assert "inquiry-when-0" in node_ids
    assert "inquiry-when-1" in node_ids
    assert "inquiry-whern" in node_ids
    assert "af.ideon.inquiry-expression/v1" in graph.contracts
    assert any(
        edge.source == "inquiry-when"
        and edge.relation == "contains-inquiry-member"
        and edge.target == "inquiry-when-0"
        for edge in graph.edges
    )


def test_explicit_when_operator_is_preserved() -> None:
    runtime = create_reference_ideon_runtime()
    record = runtime.controller.interpret(
        _request(
            {
                "actor": "alice",
                "domain": "river",
                "time": "2026-07-23T10:00:00+00:00",
                "when": "event:door-opens",
                "when-operator": "sequence",
                "where": "river-bank",
            }
        )
    )

    result = record.inquiries.get(Question.WHEN)
    assert isinstance(result, StructuredInquiryResult)
    assert isinstance(result.expression, QGroup)
    assert result.expression.operator is QOperator.SEQUENCE
    assert result.status is InquiryStatus.SATISFIED


def test_invalid_when_operator_remains_a_material_opening_and_propagates_to_whern() -> None:
    runtime = create_reference_ideon_runtime()
    record = runtime.controller.interpret(
        _request(
            {
                "actor": "alice",
                "domain": "river",
                "time": "2026-07-23T10:00:00+00:00",
                "when": "event:door-opens",
                "when-operator": "banana",
                "where": "river-bank",
            }
        )
    )

    when = record.inquiries.get(Question.WHEN)
    assert isinstance(when, StructuredInquiryResult)
    assert when.status is InquiryStatus.OPEN
    assert when.material is True
    assert "invalid:when-operator=banana" in when.evidence

    whern = record.inquiries.get(Question.WHERN)
    assert isinstance(whern, StructuredInquiryResult)
    assert whern.status is InquiryStatus.OPEN
    assert record.decision.question is Question.WHEN


def test_who_multiple_roles_form_an_all_group() -> None:
    runtime = create_reference_ideon_runtime()
    forms = runtime.service.encode_forms("sat")
    candidates = runtime.service.lookup_senses(forms, ContextGraph())
    flat = runtime.service._inquiry.query(
        forms,
        candidates,
        ContextGraph.from_dict({"actor": "alice"}),
    )
    structured = structure_inquiries(flat, ContextGraph.from_dict({"actor": "alice"}))

    who = structured.get(Question.WHO)
    assert isinstance(who, StructuredInquiryResult)
    assert isinstance(who.expression, QLeaf)
    assert who.expression.question is Question.WHO
