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
                (``expression`` field contains a validated relational expression of the form
                ``before:<X>``, ``after:<X>``, or ``during:<X>`` where the operator is
                exactly one of ``before``, ``after``, or ``during`` and ``<X>`` is a
                non-empty, non-whitespace anchor; expressions with unknown operators or
                empty/whitespace-only anchors are rejected by both the parser and the
                domain invariant).
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


#: Ordering operators accepted for ``WhenKind.ORDERING`` expressions.
_ORDERING_OPERATORS: frozenset[str] = frozenset({"before", "after", "during"})


def _validate_ordering_expression(expression: str) -> None:
    """Validate an ORDERING expression of the form ``operator:anchor``.

    Raises ``ValueError`` if the expression does not contain a ``:``, has an empty
    operator (colon at the start), the operator is not one of ``before``, ``after``, or
    ``during``, or the anchor is absent or consists only of whitespace.  This is the
    authoritative domain invariant enforced by both ``WhenCondition.__post_init__`` and
    ``parse_when_expression``.
    """
    sep = expression.find(":")
    if sep <= 0:
        # sep == -1: no ':' found; sep == 0: empty operator (starts with ':')
        raise ValueError(
            "ORDERING expression must be 'operator:anchor'; "
            "separator ':' missing or operator is empty"
        )
    operator = expression[:sep]
    anchor = expression[sep + 1 :]
    if operator not in _ORDERING_OPERATORS:
        raise ValueError(
            f"ORDERING operator must be one of {sorted(_ORDERING_OPERATORS)!r}; got {operator!r}"
        )
    if not anchor.strip():
        raise ValueError("ORDERING expression anchor must be non-empty and non-whitespace")


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
        elif self.kind in {WhenKind.EVENT, WhenKind.CRITERION}:
            if not self.expression or not self.expression.strip():
                raise ValueError(
                    f"{self.kind} WhenCondition requires a non-empty, non-whitespace expression"
                )
        elif self.kind is WhenKind.ORDERING:
            if not self.expression:
                raise ValueError(f"{self.kind} WhenCondition requires a non-empty expression")
            _validate_ordering_expression(self.expression)
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


class TemporalPerspectiveStatus(StrEnum):
    """Resolution status of a temporal perspective attached to a proposition or relation.

    Separates *whether* a standpoint was determined from *which* tense it resolved to.
    ``RESOLVED`` means a definite tense (``WAS``, ``IS``, or ``PROJECTED``) was computed
    by ``render_tense``.  ``UNANCHORED`` means the WHEN condition is structurally present
    but no reference perspective was declared, so no tense standpoint can be asserted.
    ``CONTESTED`` means a reference was supplied but the kinds were incompatible or
    uncomparable, leaving the standpoint unresolvable.

    Invariant: ``RESOLVED`` requires a non-None ``tense`` field; ``UNANCHORED`` and
    ``CONTESTED`` must not carry a ``tense``.
    """

    RESOLVED = "resolved"
    UNANCHORED = "unanchored"
    CONTESTED = "contested"


@dataclass(frozen=True, slots=True)
class TemporalPerspective:
    """Typed temporal perspective for graph construction, covering all three resolution states.

    Use this in application code instead of raw strings when the tense status is not yet
    fully resolved.  Exactly one of the following is true:
    - ``status=RESOLVED`` and ``tense`` is one of ``WAS``, ``IS``, ``PROJECTED``.
    - ``status=UNANCHORED`` and ``tense`` is ``None`` (no reference declared).
    - ``status=CONTESTED`` and ``tense`` is ``None`` (reference incompatible or uncomparable).

    ``when`` is always the ``WhenCondition`` that satisfied WHEN.  Only serialize the
    validated finite projection (``tense_label`` and ``status_label``) into graph
    properties; do not pass raw strings from outside the domain.
    """

    status: TemporalPerspectiveStatus
    when: WhenCondition
    tense: Tense | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, TemporalPerspectiveStatus):
            raise TypeError("status must be a TemporalPerspectiveStatus")
        if not isinstance(self.when, WhenCondition):
            raise TypeError("when must be a WhenCondition")
        if self.status is TemporalPerspectiveStatus.RESOLVED:
            if self.tense is None:
                raise ValueError("RESOLVED TemporalPerspective requires a non-None tense")
            if not isinstance(self.tense, Tense):
                raise TypeError("tense must be a Tense when status is RESOLVED")
        else:
            if self.tense is not None:
                raise ValueError(
                    f"{self.status.value} TemporalPerspective must not carry a tense "
                    "(only RESOLVED perspectives carry a resolved tense)"
                )

    @property
    def tense_label(self) -> str:
        """Serialisable tense label for graph properties.

        For ``RESOLVED`` perspectives returns the tense value (``"was"``, ``"is"``,
        ``"projected"``).  For unresolved perspectives returns the status value
        (``"unanchored"`` or ``"contested"``), giving a single finite property that
        carries the complete standpoint representation.
        """
        return self.tense.value if self.tense is not None else self.status.value

    @property
    def status_label(self) -> str:
        """Serialisable resolution-status label for graph properties (``tense-status``)."""
        return self.status.value


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
    - ``ordering:<op>:<anchor>`` — a bounded ordering relation: ``op`` must be one of
                                   ``before``, ``after``, or ``during``; ``<anchor>``
                                   must be a non-empty, non-whitespace reference point
                                   (e.g. ``ordering:before:release-v1``)

    Fail-closed: any string that does not match one of these patterns exactly, has an
    unknown operator, an empty/whitespace-only anchor, or an empty expression after the
    prefix returns ``None`` so that WHEN is not satisfied merely because ``context.when``
    is a non-empty arbitrary string.
    """
    sep = value.find(":")
    if sep < 1:
        return None
    kind_str = value[:sep]
    payload = value[sep + 1 :]
    if kind_str not in _WHEN_EXPRESSION_KINDS:
        return None
    if kind_str in {"event", "criterion"}:
        if not payload or not payload.strip():
            return None
        return WhenCondition(WhenKind(kind_str), expression=payload)
    if kind_str == "ordering":
        if not payload:
            return None
        # Validate structured format: operator:anchor where operator is known and anchor
        # is non-empty, non-whitespace.  _validate_ordering_expression enforces the same
        # invariant at the domain level; the parser validates first (fail-closed layer 1).
        op_sep = payload.find(":")
        if op_sep <= 0:
            # -1: no separator found; 0: empty operator (leading colon)
            return None
        operator = payload[:op_sep]
        anchor = payload[op_sep + 1 :]
        if operator not in _ORDERING_OPERATORS:
            return None
        if not anchor.strip():
            return None
        return WhenCondition(WhenKind.ORDERING, expression=payload)
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
