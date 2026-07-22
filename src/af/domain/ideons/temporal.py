"""Pure Whern and temporal-perspective rules.

Temporal modality attaches to propositions, relations, and context rather than being
encoded as a privileged inquiry attention.  The three tense values map directly to the
``WAS < IS > PROJECTED`` qualification axis: past, present, and future/projected states.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Tense(StrEnum):
    """Temporal perspective relative to a declared reference Whern.

    ``WAS`` and ``IS`` record attested states; ``PROJECTED`` represents a future or
    projected state.  All three qualify propositions and relations in context rather
    than serving as an isolated inquiry attention.
    """

    WAS = "was"
    IS = "is"
    PROJECTED = "projected"


class AuxiliaryKind(StrEnum):
    """Semantic category of a resolved auxiliary relation, not the surface token.

    Use this to classify the *resolved* relation name (e.g. ``future-state``,
    ``intends``) after dictionary disambiguation, not the ambiguous surface form.
    """

    TEMPORAL = "temporal"
    VOLITIONAL = "volitional"


@dataclass(frozen=True, slots=True)
class TemporalQualification:
    """A tense qualification that attaches to a proposition or relation node.

    Attach an instance of this to a graph node's context rather than treating
    past/present/future modality as a separate inquiry question.
    """

    tense: Tense
    whern: Whern

    def __post_init__(self) -> None:
        if not isinstance(self.tense, Tense):
            raise TypeError("tense must be a Tense")
        if not isinstance(self.whern, Whern):
            raise TypeError("whern must be a Whern")


@dataclass(frozen=True, slots=True)
class Whern:
    temporal_anchor: datetime | None
    reference_perspective: datetime | None
    criterion: str = "now = datetime_provided"
    status: str = "declared"

    def __post_init__(self) -> None:
        for label, value in (
            ("temporal anchor", self.temporal_anchor),
            ("reference perspective", self.reference_perspective),
        ):
            if value is not None and value.tzinfo is None:
                raise ValueError(f"{label} must be timezone-aware")
        if not self.criterion:
            raise ValueError("Whern criterion must not be empty")
        if not self.status:
            raise ValueError("Whern status must not be empty")


def render_tense(event: Whern, reference: Whern) -> Tense:
    """Render the tense of *event* relative to a *reference* Whern.

    Returns ``WAS`` when the event precedes the reference perspective, ``IS`` when
    they coincide, and ``PROJECTED`` when the event follows it.  The result qualifies
    the proposition or relation that the event Whern is attached to.
    """
    event_time = event.temporal_anchor
    reference_time = reference.reference_perspective or reference.temporal_anchor
    if event_time is None or reference_time is None:
        if event.criterion == reference.criterion and event.status == "satisfied":
            return Tense.IS
        raise ValueError("tense rendering requires comparable Whern anchors or criteria")
    if event_time < reference_time:
        return Tense.WAS
    if event_time > reference_time:
        return Tense.PROJECTED
    return Tense.IS


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
