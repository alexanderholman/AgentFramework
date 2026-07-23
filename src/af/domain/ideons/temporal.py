"""Temporal model: WhenCondition, Whern (WHERE+WHEN composite), and tense rules.

Temporal modality attaches to propositions, relations, and context rather than being
encoded as a privileged inquiry attention.  The three tense values map directly to the
``WAS < IS > PROJECTED`` qualification axis: past, present, and future/projected states.

``WHEN(P)`` — under what temporal or contextual condition does/did P hold?  A timestamp
is one possible witness; WHEN also supports intervals, event/state predicates, condition
clauses, and ordering relations (``before``, ``after``, ``during`` relative to a named
event or anchor).

``WHERN(P)`` — in what combined place-and-time/condition does/did P hold?  WHERN is the
conjunction of a WHERE (spatial/domain) component and a WHEN (temporal/conditional)
component.  It is satisfied only when both roles are present.

Global tense qualification (``WAS < IS > PROJECTED``) attaches to propositions and
relations via ``TemporalQualification``.  The surface token ``will`` is disambiguated
through the dictionary into a temporal relation (WHEN slot) or a volitional action; it
does not create a privileged inquiry attention.

``parse_when_expression`` is the authoritative public parser for the bounded
``kind:expression`` WHEN context format used by both InquiryEngine and graph construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Tense(StrEnum):
    """Temporal perspective of a proposition relative to a declared temporal standpoint.

    ``render_tense`` compares a ``WhenCondition`` against a reference perspective to
    yield one of these three values.  ``WAS`` and ``IS`` record attested states;
    ``PROJECTED`` represents a future or projected state.  All three qualify propositions
    and relations in context rather than serving as an isolated inquiry attention.
    """

    WAS = "was"
    IS = "is"
    PROJECTED = "projected"


class WhenKind(StrEnum):
    """Typed alternatives for the condition under which a proposition holds.

    TIMESTAMP — an instant in time (``instant`` field is set).
    INTERVAL  — a time range (``interval_start``/``interval_end`` fields; at least one
                bound must be present, and when both are present start must not exceed
                end).
    EVENT     — an event or state predicate label (``expression`` field is set).
    CRITERION — a condition clause or transition expression (``expression`` field is set).
    ORDERING  — a bounded ordering relation relative to a named event or anchor
                (``expression`` field contains a relational expression of the form
                ``before:<X>``, ``after:<X>``, or ``during:<X>`` where ``<X>`` identifies
                the reference point; any non-empty relational expression is accepted).
    """

    TIMESTAMP = "timestamp"
    INTERVAL = "interval"
    EVENT = "event"
    CRITERION = "criterion"
    ORDERING = "ordering"


class AuxiliaryKind(StrEnum):
    """Semantic category of a resolved auxiliary relation, not the surface token.

    Use this to classify the *resolved* relation name (e.g. ``future-state``,
    ``intends``) after dictionary disambiguation, not the ambiguous surface form.
    """

    TEMPORAL = "temporal"
    VOLITIONAL = "volitional"


@dataclass(frozen=True, slots=True)
class WhenCondition:
    """Condition-bearing temporal relation under which a proposition holds/held/becomes true.

    Supports timestamp (instant), interval (start/end), event/state predicate label,
    and condition clause/transition expression as typed alternatives.  A timestamp is one
    possible witness; WHEN is not reducible to a scalar datetime field.

    Use this as the temporal/conditional role in ``TemporalQualification`` and as the
    ``when`` component of ``Whern``.
    """

    kind: WhenKind
    instant: datetime | None = None
    interval_start: datetime | None = None
    interval_end: datetime | None = None
    expression: str | None = None
    status: str = "declared"

    def __post_init__(self) -> None:
        if not isinstance(self.kind, WhenKind):
            raise TypeError("kind must be a WhenKind")
        if self.kind is WhenKind.TIMESTAMP:
            if self.instant is None:
                raise ValueError("TIMESTAMP WhenCondition requires an instant")
            if self.instant.tzinfo is None:
                raise ValueError("WhenCondition instant must be timezone-aware")
        elif self.kind is WhenKind.INTERVAL:
            for label, value in (
                ("interval_start", self.interval_start),
                ("interval_end", self.interval_end),
            ):
                if value is not None and value.tzinfo is None:
                    raise ValueError(f"WhenCondition {label} must be timezone-aware")
            if self.interval_start is None and self.interval_end is None:
                raise ValueError(
                    "INTERVAL WhenCondition requires at least one bound "
                    "(interval_start or interval_end must be set)"
                )
            if (
                self.interval_start is not None
                and self.interval_end is not None
                and self.interval_start > self.interval_end
            ):
                raise ValueError(
                    "INTERVAL WhenCondition interval_start must not exceed interval_end"
                )
        elif self.kind in {WhenKind.EVENT, WhenKind.CRITERION, WhenKind.ORDERING}:
            if not self.expression:
                raise ValueError(f"{self.kind} WhenCondition requires a non-empty expression")
        if not self.status:
            raise ValueError("WhenCondition status must not be empty")


@dataclass(frozen=True, slots=True)
class Whern:
    """Composite spatiotemporal constraint: the conjunction of WHERE and WHEN components.

    ``WHERN(P)`` asks: in what combined place-and-time/condition does/did P hold?
    It is satisfied only when both the spatial/domain (``where``) and the
    temporal/conditional (``when``) roles are present and satisfied.

    ``WHEN(P)`` alone — ``WhenCondition`` — asks under what temporal or contextual
    condition P holds.  ``WHERE(P)`` alone identifies the spatial or domain context.
    ``WHERN`` is their composition, not a synonym for either individually.
    """

    when: WhenCondition | None = None
    where: str | None = None


@dataclass(frozen=True, slots=True)
class TemporalQualification:
    """A tense qualification that attaches to a proposition or relation node.

    Attach an instance of this to a graph node's context rather than treating
    past/present/future modality as a separate inquiry question.
    """

    tense: Tense
    when: WhenCondition

    def __post_init__(self) -> None:
        if not isinstance(self.tense, Tense):
            raise TypeError("tense must be a Tense")
        if not isinstance(self.when, WhenCondition):
            raise TypeError("when must be a WhenCondition")


def render_tense(event: WhenCondition, reference: WhenCondition) -> Tense:
    """Render the tense of *event* relative to a *reference* temporal perspective.

    Both arguments must be comparable ``WhenCondition`` instances of the same kind.
    For ``TIMESTAMP`` conditions, the instants are compared directly.  For ``CRITERION``
    conditions, matching expressions with ``status='satisfied'`` return ``IS``.
    The result qualifies the proposition or relation attached to the event condition.
    """
    if event.kind is WhenKind.TIMESTAMP and reference.kind is WhenKind.TIMESTAMP:
        assert event.instant is not None and reference.instant is not None
        if event.instant < reference.instant:
            return Tense.WAS
        if event.instant > reference.instant:
            return Tense.PROJECTED
        return Tense.IS
    if event.kind is WhenKind.CRITERION and reference.kind is WhenKind.CRITERION:
        if event.expression == reference.expression and event.status == "satisfied":
            return Tense.IS
        raise ValueError(
            "criterion tense rendering requires matching expressions and satisfied status"
        )
    raise ValueError(
        "tense rendering requires comparable WhenCondition kinds; "
        f"got {event.kind!r} and {reference.kind!r}"
    )


def auxiliary_kind(relation: str) -> AuxiliaryKind:
    """Classify a resolved relation, not the ambiguous surface token.

    Pass the *resolved* relation name after dictionary disambiguation (e.g.
    ``future-state`` or ``intends``), not the raw surface form.
    """
    if relation in {"future-state", "future-event"}:
        return AuxiliaryKind.TEMPORAL
    if relation in {"intends", "wants", "commits"}:
        return AuxiliaryKind.VOLITIONAL
    raise ValueError(f"unresolved auxiliary relation: {relation!r}")


#: Accepted ``kind`` prefixes for the bounded ``kind:expression`` WHEN context format.
_WHEN_EXPRESSION_KINDS: frozenset[str] = frozenset({"event", "criterion", "interval", "ordering"})


def parse_when_expression(value: str) -> WhenCondition | None:
    """Parse a bounded ``kind:expression`` string into a ``WhenCondition``.

    This is the authoritative public parser for WHEN context fields consumed by both
    ``InquiryEngine`` and ``IdeonService.construct_graph``.  Callers must import and
    use this function; private copies or re-implementations are not permitted.

    Accepted formats:
    - ``event:<expression>``     — an event or state predicate label
    - ``criterion:<expression>`` — a condition clause or transition expression
    - ``interval:<start>,<end>`` — an interval with at least one timezone-aware
                                   ISO 8601 bound (both open is invalid)
    - ``ordering:<expression>``  — a bounded ordering relation relative to a named
                                   event or anchor (e.g. ``ordering:before:release-v1``)

    Fail-closed: any string that does not match one of these patterns, or has an empty
    expression after the prefix, returns ``None`` so that WHEN is not satisfied merely
    because ``context.when`` is a non-empty arbitrary string.
    """
    sep = value.find(":")
    if sep < 1:
        return None
    kind_str = value[:sep]
    payload = value[sep + 1 :]
    if kind_str not in _WHEN_EXPRESSION_KINDS:
        return None
    if kind_str in {"event", "criterion", "ordering"}:
        if not payload:
            return None
        return WhenCondition(WhenKind(kind_str), expression=payload)
    # interval: "<start>,<end>" where at least one must be a timezone-aware ISO timestamp
    comma = payload.find(",")
    if comma < 0:
        return None
    raw_start = payload[:comma]
    raw_end = payload[comma + 1 :]
    interval_start: datetime | None = None
    interval_end: datetime | None = None
    if raw_start:
        try:
            parsed = datetime.fromisoformat(raw_start)
        except (ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        interval_start = parsed
    if raw_end:
        try:
            parsed = datetime.fromisoformat(raw_end)
        except (ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        interval_end = parsed
    if interval_start is None and interval_end is None:
        return None  # both open — not a valid bounded interval
    # Fail-closed at parse time: start must not exceed end.  WhenCondition.__post_init__
    # enforces the same invariant unconditionally as the authoritative second layer.
    if interval_start is not None and interval_end is not None and interval_start > interval_end:
        return None
    return WhenCondition(
        WhenKind.INTERVAL,
        interval_start=interval_start,
        interval_end=interval_end,
    )
