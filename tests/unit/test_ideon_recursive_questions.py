from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from af.domain.ideons.questions import (
    MAX_Q_DEPTH,
    QGroup,
    QLeaf,
    QOperator,
    expression_depth,
    q_expression_bytes,
    q_expression_id,
)
from af.domain.ideons.records import InquiryStatus, Question
from af.domain.ideons.spacetime import SpacetimeEventRef, WhenMark


def leaf(question: Question, value_ref: str, *, role: str) -> QLeaf:
    return QLeaf(question, "ideon-ref", value_ref, role=role)


def test_when_collection_is_recursive_without_making_recursion_temporal_only() -> None:
    timestamp = leaf(Question.WHEN, "when-mark:clock-a:42", role="clock-mark")
    door_opens = leaf(Question.WHEN, "event:door-opens", role="event-condition")
    all_conditions = QGroup(
        Question.WHEN,
        QOperator.ALL,
        (timestamp, door_opens),
        InquiryStatus.SATISFIED,
        role="temporal-condition",
    )
    sequence = QGroup(
        Question.WHEN,
        QOperator.SEQUENCE,
        (all_conditions, leaf(Question.WHEN, "event:light-on", role="resulting-event")),
        InquiryStatus.SATISFIED,
    )

    assert expression_depth(sequence) == 3
    assert q_expression_bytes(sequence) == q_expression_bytes(sequence)
    assert len(q_expression_id(sequence)) == 64


def test_non_temporal_questions_use_the_same_recursive_contract() -> None:
    speaker = leaf(Question.WHO, "person:alice", role="speaker")
    affected = leaf(Question.WHO, "person:bob", role="affected")
    who = QGroup(
        Question.WHO,
        QOperator.ALL,
        (speaker, affected),
        InquiryStatus.SATISFIED,
        role="participants",
    )
    why = QGroup(
        Question.WHY,
        QOperator.TRIGGER,
        (
            leaf(Question.WHY, "event:door-opens", role="trigger"),
            leaf(Question.WHY, "state:light-on", role="consequence"),
        ),
        InquiryStatus.SATISFIED,
        role="causal-explanation",
    )

    assert expression_depth(who) == 2
    assert expression_depth(why) == 2
    assert who.operator is QOperator.ALL
    assert why.operator is QOperator.TRIGGER


def test_whern_recursively_amalgamates_where_and_when() -> None:
    where = leaf(Question.WHERE, "place:river-bank", role="location")
    when = QGroup(
        Question.WHEN,
        QOperator.ALL,
        (
            leaf(Question.WHEN, "when-mark:clock-a:42", role="clock-mark"),
            leaf(Question.WHEN, "criterion:door-open", role="criterion"),
        ),
        InquiryStatus.SATISFIED,
    )
    whern = QGroup(
        Question.WHERN,
        QOperator.ALL,
        (where, when),
        InquiryStatus.SATISFIED,
        role="spatiotemporal-condition",
    )

    assert whern.members[0].question is Question.WHERE
    assert whern.members[1].question is Question.WHEN
    assert expression_depth(whern) == 3


def test_recursive_question_depth_is_bounded() -> None:
    expression: QLeaf | QGroup = leaf(Question.WHAT, "thing:root", role="value")
    for _ in range(MAX_Q_DEPTH - 1):
        expression = QGroup(
            Question.WHAT,
            QOperator.ALL,
            (expression,),
            InquiryStatus.SATISFIED,
        )

    assert expression_depth(expression) == MAX_Q_DEPTH
    with pytest.raises(ValueError, match="maximum depth"):
        QGroup(
            Question.WHAT,
            QOperator.ALL,
            (expression,),
            InquiryStatus.SATISFIED,
        )


def event_ref(*, position: str, velocity: str, state: str) -> SpacetimeEventRef:
    return SpacetimeEventRef(
        position_ref=position,
        velocity_ref=velocity,
        frame_ref="frame:itrf-v1",
        metric_model_ref="metric:earth-reference-v1",
        local_state_ref=state,
    )


def test_when_mark_binds_clock_position_velocity_and_local_state() -> None:
    utc = datetime(2026, 7, 23, 10, 40, 57, tzinfo=UTC)
    first = WhenMark(
        clock_ref="clock:atomic-a",
        worldline_ref="worldline:clock-a",
        proper_time_seconds=Decimal("123456.000000001"),
        event=event_ref(
            position="position:lab-a:42",
            velocity="velocity:lab-a:42",
            state="state:lab-a:42",
        ),
        uncertainty_seconds=Decimal("0.000000001"),
        provenance=("observation:sensor-a:42",),
        utc_projection=utc,
    )
    changed_state = WhenMark(
        clock_ref="clock:atomic-a",
        worldline_ref="worldline:clock-a",
        proper_time_seconds=Decimal("123456.000000001"),
        event=event_ref(
            position="position:lab-a:42",
            velocity="velocity:lab-a:42",
            state="state:lab-a:43",
        ),
        uncertainty_seconds=Decimal("0.000000001"),
        provenance=("observation:sensor-a:42",),
        utc_projection=utc,
    )

    assert first.utc_projection == changed_state.utc_projection
    assert first.mark_id != changed_state.mark_id
    assert first.to_record()["event"]["local_state_ref"] == "state:lab-a:42"


def test_when_mark_fails_closed_on_invalid_physical_record() -> None:
    event = event_ref(
        position="position:lab-a:42",
        velocity="velocity:lab-a:42",
        state="state:lab-a:42",
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        WhenMark(
            clock_ref="clock:atomic-a",
            worldline_ref="worldline:clock-a",
            proper_time_seconds=Decimal("1"),
            event=event,
            uncertainty_seconds=Decimal("0"),
            provenance=("observation:1",),
            utc_projection=datetime(2026, 7, 23, 10, 40, 57),
        )
    with pytest.raises(ValueError, match="must not be negative"):
        WhenMark(
            clock_ref="clock:atomic-a",
            worldline_ref="worldline:clock-a",
            proper_time_seconds=Decimal("1"),
            event=event,
            uncertainty_seconds=Decimal("-0.1"),
            provenance=("observation:1",),
        )
