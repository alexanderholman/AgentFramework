"""Authoritative recursive inquiry integration for the Ideon reference runtime."""

from __future__ import annotations

import json

from af.application.ideons import IdeonService, InterpretRequest
from af.domain.ideons.models import (
    IdeonEdge,
    IdeonGraph,
    IdeonIdentity,
    IdeonNode,
    NodeKind,
    Property,
)
from af.domain.ideons.numeric import natural_to_decimal
from af.domain.ideons.questions import (
    QExpression,
    QGroup,
    QLeaf,
    QOperator,
    q_expression_id,
)
from af.domain.ideons.records import (
    CandidateSet,
    ContextGraph,
    FormSequence,
    InquiryResult,
    InquiryState,
    InquiryStatus,
    InterpretationRecord,
    Question,
    ShouldAssessment,
)
from af.domain.ideons.structured import (
    StructuredInquiryResult,
    StructuredInterpretationRecord,
    structured_record,
)
from af.domain.ideons.temporal import WhenCondition

_OPERATOR_BY_VALUE: dict[str, QOperator] = {operator.value: operator for operator in QOperator}
_LEGACY_TEMPORAL_NODE_ID = "temporal-qualification"


def _leaf_for_result(result: InquiryResult) -> QLeaf:
    if result.candidate_keys:
        value_kind = "candidate-set"
        value_ref = ",".join(natural_to_decimal(key) for key in result.candidate_keys)
    elif result.required_information is not None:
        value_kind = "opening"
        value_ref = result.required_information
    elif result.evidence:
        value_kind = "evidence"
        value_ref = result.evidence[0]
    else:
        value_kind = "status"
        value_ref = result.status.value
    return QLeaf(
        question=result.question,
        value_kind=value_kind,
        value_ref=value_ref,
        status=result.status,
        role=result.role,
        evidence=result.evidence,
    )


def _who_expression(result: InquiryResult) -> QExpression:
    role_members: list[QLeaf] = []
    for evidence in result.evidence:
        if evidence.startswith("context:"):
            role_members.append(
                QLeaf(
                    Question.WHO,
                    "context-role",
                    evidence.removeprefix("context:"),
                    InquiryStatus.SATISFIED,
                    evidence=(evidence,),
                )
            )
        elif evidence.startswith("missing:"):
            role_members.append(
                QLeaf(
                    Question.WHO,
                    "unfilled-role",
                    evidence.removeprefix("missing:"),
                    InquiryStatus.OPEN,
                    evidence=(evidence,),
                )
            )
    if len(role_members) <= 1:
        return role_members[0] if role_members else _leaf_for_result(result)
    return QGroup(
        Question.WHO,
        QOperator.ALL,
        tuple(role_members),
        result.status,
        role=result.role,
        evidence=result.evidence,
    )


def _where_expression(result: InquiryResult) -> QExpression:
    if len(result.candidate_keys) > 1:
        members = tuple(
            QLeaf(
                Question.WHERE,
                "candidate-ideon-key",
                natural_to_decimal(key),
                InquiryStatus.SATISFIED,
                role="location-candidate",
            )
            for key in result.candidate_keys
        )
        return QGroup(
            Question.WHERE,
            QOperator.ANY,
            members,
            result.status,
            role=result.role,
            evidence=result.evidence,
        )
    return _leaf_for_result(result)


def _when_leaf(evidence: str) -> QLeaf:
    if evidence.startswith("context:time="):
        return QLeaf(
            Question.WHEN,
            "timestamp",
            evidence.removeprefix("context:time="),
            InquiryStatus.SATISFIED,
            role="temporal-mark",
            evidence=(evidence,),
        )
    raw = evidence.removeprefix("context:when=")
    kind, separator, _ = raw.partition(":")
    return QLeaf(
        Question.WHEN,
        kind if separator else "condition",
        raw,
        InquiryStatus.SATISFIED,
        role="temporal-condition",
        evidence=(evidence,),
    )


def _when_expression(result: InquiryResult, context: ContextGraph) -> QExpression:
    members = tuple(
        _when_leaf(evidence)
        for evidence in result.evidence
        if evidence.startswith(("context:time=", "context:when="))
    )
    if not members:
        return _leaf_for_result(result)
    if len(members) == 1:
        return members[0]

    # Context fields describe one jointly applicable context by default. Callers may
    # declare another bounded composition explicitly through ``when-operator``.
    operator_value = context.get("when-operator") or QOperator.ALL.value
    operator = _OPERATOR_BY_VALUE.get(operator_value)
    if operator is None:
        return QGroup(
            Question.WHEN,
            QOperator.ALL,
            members,
            InquiryStatus.OPEN,
            role="temporal-composition",
            evidence=(*result.evidence, f"invalid:when-operator={operator_value}"),
        )
    return QGroup(
        Question.WHEN,
        operator,
        members,
        result.status,
        role=result.role,
        evidence=result.evidence,
    )


def _whern_status(where: QExpression, when: QExpression) -> InquiryStatus:
    statuses = (where.status, when.status)
    if statuses == (InquiryStatus.NOT_APPLICABLE, InquiryStatus.NOT_APPLICABLE):
        return InquiryStatus.NOT_APPLICABLE
    if InquiryStatus.CONTESTED in statuses:
        return InquiryStatus.CONTESTED
    if InquiryStatus.OPEN in statuses or InquiryStatus.NOT_APPLICABLE in statuses:
        return InquiryStatus.OPEN
    return InquiryStatus.SATISFIED


def _structured_result(result: InquiryResult, expression: QExpression) -> StructuredInquiryResult:
    # An invalid explicit operator is an unresolved material opening, even though the
    # compatibility engine could validate each constituent independently.
    status = expression.status
    temporal_composition_open = result.question is Question.WHEN and status is InquiryStatus.OPEN
    material = result.material or temporal_composition_open
    required_information = result.required_information
    if temporal_composition_open and required_information is None:
        required_information = "the composition operator joining temporal conditions"
    elif (
        result.question is Question.WHERN
        and status is InquiryStatus.OPEN
        and required_information is None
    ):
        required_information = "resolved WHERE and WHEN constituents for WHERN"
    return StructuredInquiryResult(
        question=result.question,
        status=status,
        role=result.role,
        candidate_keys=result.candidate_keys,
        required_information=required_information,
        material=material,
        evidence=expression.evidence,
        expression=expression,
    )


def structure_inquiries(state: InquiryState, context: ContextGraph) -> InquiryState:
    """Convert flat compatibility results into authoritative recursive values."""

    expressions: dict[Question, QExpression] = {}
    structured: list[StructuredInquiryResult] = []
    for result in state.results:
        if result.question is Question.WHO:
            expression = _who_expression(result)
        elif result.question is Question.WHERE:
            expression = _where_expression(result)
        elif result.question is Question.WHEN:
            expression = _when_expression(result, context)
        elif result.question is Question.WHERN:
            where = expressions[Question.WHERE]
            when = expressions[Question.WHEN]
            constituents = tuple(
                expression
                for expression in (where, when)
                if expression.status is not InquiryStatus.NOT_APPLICABLE
            )
            expression = (
                QGroup(
                    Question.WHERN,
                    QOperator.ALL,
                    constituents,
                    _whern_status(where, when),
                    role=result.role,
                    evidence=result.evidence,
                )
                if constituents
                else _leaf_for_result(result)
            )
        else:
            expression = _leaf_for_result(result)
        expressions[result.question] = expression
        structured.append(_structured_result(result, expression))
    return InquiryState(tuple(structured))


def _node_properties(expression: QExpression) -> tuple[Property, ...]:
    common = [
        Property("expression-id", q_expression_id(expression)),
        Property("expression-kind", "group" if isinstance(expression, QGroup) else "leaf"),
        Property("question", expression.question.value),
        Property("status", expression.status.value),
    ]
    if expression.role is not None:
        common.append(Property("role", expression.role))
    if expression.evidence:
        common.append(
            Property(
                "evidence-json",
                json.dumps(
                    list(expression.evidence),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        )
    if isinstance(expression, QLeaf):
        common.extend(
            (
                Property("value-kind", expression.value_kind),
                Property("value-ref", expression.value_ref),
            )
        )
    else:
        common.append(Property("operator", expression.operator.value))
    return tuple(common)


def _project_expression(
    expression: QExpression,
    node_id: str,
) -> tuple[list[IdeonNode], list[IdeonEdge]]:
    nodes = [
        IdeonNode(
            node_id,
            NodeKind.RELATION if isinstance(expression, QGroup) else NodeKind.VALUE,
            properties=_node_properties(expression),
        )
    ]
    edges: list[IdeonEdge] = []
    if isinstance(expression, QGroup):
        for index, member in enumerate(expression.members):
            child_id = f"{node_id}-{index}"
            child_nodes, child_edges = _project_expression(member, child_id)
            nodes.extend(child_nodes)
            edges.extend(child_edges)
            edges.append(
                IdeonEdge(
                    source=node_id,
                    relation="contains-inquiry-member",
                    target=child_id,
                    source_role="inquiry-group",
                    target_role="member",
                    context=(Property("member-index", index),),
                )
            )
    return nodes, edges


def project_inquiry_expressions(
    inquiry: InquiryState,
) -> tuple[tuple[IdeonNode, ...], tuple[IdeonEdge, ...]]:
    """Project every authoritative expression into typed graph nodes and edges."""

    nodes: list[IdeonNode] = []
    edges: list[IdeonEdge] = []
    for result in inquiry.results:
        if not isinstance(result, StructuredInquiryResult) or result.expression is None:
            raise TypeError("graph projection requires structured inquiry results")
        result_nodes, result_edges = _project_expression(
            result.expression,
            f"inquiry-{result.question.value}",
        )
        nodes.extend(result_nodes)
        edges.extend(result_edges)
    return tuple(nodes), tuple(edges)


def _expression_identity_properties(inquiry: InquiryState) -> tuple[Property, ...]:
    properties: list[Property] = []
    for result in inquiry.results:
        if not isinstance(result, StructuredInquiryResult) or result.expression is None:
            raise TypeError("identity construction requires structured inquiry results")
        properties.append(
            Property(
                f"inquiry-expression-{result.question.value}",
                q_expression_id(result.expression),
            )
        )
    return tuple(properties)


def _predicate_node_ids(candidates: tuple[CandidateSet, ...]) -> tuple[str, ...]:
    node_ids: list[str] = []
    for form_index, candidate_set in enumerate(candidates):
        for candidate_index, candidate in enumerate(candidate_set.viable):
            if candidate.entry.semantic_type in {"action", "state", "relation"}:
                node_ids.append(f"sense-{form_index}-{candidate_index}")
    return tuple(node_ids)


def _reconcile_temporal_projection(
    graph: IdeonGraph,
    inquiry: InquiryState,
) -> tuple[tuple[IdeonNode, ...], tuple[IdeonEdge, ...]]:
    """Keep global perspective for one WHEN leaf, remove it for composed WHEN values.

    The base service's temporal node still carries useful global WAS / IS / PROJECTED
    perspective for one condition relative to a supplied reference.  For a recursive WHEN
    group, however, that flat projection can only represent one member and historically
    marked ordinary multiplicity as contested.  The recursive expression is authoritative
    for groups, so the lossy flat node and its edges are removed in that case.
    """

    when_result = inquiry.get(Question.WHEN)
    if not isinstance(when_result, StructuredInquiryResult) or when_result.expression is None:
        raise TypeError("temporal reconciliation requires a structured WHEN result")
    if not isinstance(when_result.expression, QGroup):
        return graph.nodes, graph.edges

    nodes = tuple(node for node in graph.nodes if node.node_id != _LEGACY_TEMPORAL_NODE_ID)
    edges = tuple(
        edge
        for edge in graph.edges
        if edge.source != _LEGACY_TEMPORAL_NODE_ID and edge.target != _LEGACY_TEMPORAL_NODE_ID
    )
    return nodes, edges


class StructuredIdeonService(IdeonService):
    """Ideon service whose runtime, graph, identity, and transport share one Q structure."""

    def query_openings(
        self,
        forms: FormSequence,
        candidates: tuple[CandidateSet, ...],
        context: ContextGraph,
        *,
        should: ShouldAssessment | None = None,
    ) -> InquiryState:
        return structure_inquiries(
            super().query_openings(forms, candidates, context, should=should),
            context,
        )

    def construct_graph(
        self,
        forms: FormSequence,
        candidates: tuple[CandidateSet, ...],
        context: ContextGraph,
        inquiry: InquiryState,
        *,
        source_id: str | None = None,
        rights: str | None = None,
        reference_when: WhenCondition | None = None,
    ) -> IdeonGraph:
        graph = super().construct_graph(
            forms,
            candidates,
            context,
            inquiry,
            source_id=source_id,
            rights=rights,
            reference_when=reference_when,
        )
        base_nodes, base_edges = _reconcile_temporal_projection(graph, inquiry)
        inquiry_nodes, inquiry_edges = project_inquiry_expressions(inquiry)

        when_result = inquiry.get(Question.WHEN)
        when_edges: list[IdeonEdge] = []
        if when_result.status is not InquiryStatus.NOT_APPLICABLE:
            for predicate_node_id in _predicate_node_ids(candidates):
                when_edges.append(
                    IdeonEdge(
                        source="inquiry-when",
                        relation="qualifies-predicate",
                        target=predicate_node_id,
                        source_role="when-expression",
                        target_role="predicate",
                    )
                )

        identity = IdeonIdentity(
            namespace=graph.identity.namespace,
            ruleset=graph.identity.ruleset,
            name=graph.identity.name,
            kind=graph.identity.kind,
            roles=graph.identity.roles,
            order=graph.identity.order,
            context=(
                *graph.identity.context,
                *_expression_identity_properties(inquiry),
            ),
            roots=(
                *graph.identity.roots,
                *(f"inquiry-{result.question.value}" for result in inquiry.results),
            ),
        )
        return IdeonGraph(
            identity=identity,
            nodes=(*base_nodes, *inquiry_nodes),
            edges=(*base_edges, *inquiry_edges, *when_edges),
            hyperedges=graph.hyperedges,
            contracts=(*graph.contracts, "af.ideon.inquiry-expression/v1"),
            openings=graph.openings,
            provenance=graph.provenance,
            annotations=(
                *graph.annotations,
                Property("inquiry-expression-schema", "af.ideon.inquiry-expression/v1"),
            ),
        )

    def interpret(self, request: InterpretRequest) -> StructuredInterpretationRecord:
        record: InterpretationRecord = super().interpret(request)
        return structured_record(record)
