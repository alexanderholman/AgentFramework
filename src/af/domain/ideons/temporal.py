"""Pure Whern and temporal-perspective rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Tense(StrEnum):
    WAS = "was"
    IS = "is"
    WILL = "will"


class WillKind(StrEnum):
    TEMPORAL = "temporal"
    VOLITIONAL = "volitional"


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
    event_time = event.temporal_anchor
    reference_time = reference.reference_perspective or reference.temporal_anchor
    if event_time is None or reference_time is None:
        if event.criterion == reference.criterion and event.status == "satisfied":
            return Tense.IS
        raise ValueError("tense rendering requires comparable Whern anchors or criteria")
    if event_time < reference_time:
        return Tense.WAS
    if event_time > reference_time:
        return Tense.WILL
    return Tense.IS


def will_kind(relation: str) -> WillKind:
    """Classify a resolved relation, not the ambiguous surface token ``will``."""
    if relation in {"future-state", "future-event"}:
        return WillKind.TEMPORAL
    if relation in {"intends", "wants", "commits"}:
        return WillKind.VOLITIONAL
    raise ValueError(f"unresolved will relation: {relation!r}")
