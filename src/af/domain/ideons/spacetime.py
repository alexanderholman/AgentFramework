"""Situated physical marks used to ground temporal inquiry values.

A UTC timestamp is a useful coordinate projection, not the ontological basis of a
WHEN value.  ``WhenMark`` records an atomic/local clock reading on a declared
worldline and ties it to the spacetime position, velocity, reference frame, metric
model, and bounded local-state graph at the event.  This module defines reference
contracts only; it does not claim to implement relativistic coordinate transforms.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

_MAX_REF_LENGTH = 512
_MAX_PROVENANCE_ITEMS = 64


def _reference(value: str, label: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    if not value or len(value) > _MAX_REF_LENGTH or "\x00" in value:
        raise ValueError(f"{label} must be a non-empty bounded reference")


def _finite_decimal(value: Decimal, label: str, *, non_negative: bool = False) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{label} must be a Decimal")
    if not value.is_finite():
        raise ValueError(f"{label} must be finite")
    if non_negative and value < 0:
        raise ValueError(f"{label} must not be negative")


@dataclass(frozen=True, slots=True)
class SpacetimeEventRef:
    """Immutable references needed to interpret one situated clock observation.

    The referenced records must declare their units, coordinate conventions, schema
    versions, and revisions.  ``local_state_ref`` points to the scope-bounded state of
    the relevant position at the event, not to an impossible snapshot of the universe.
    """

    position_ref: str
    velocity_ref: str
    frame_ref: str
    metric_model_ref: str
    local_state_ref: str

    def __post_init__(self) -> None:
        _reference(self.position_ref, "position reference")
        _reference(self.velocity_ref, "velocity reference")
        _reference(self.frame_ref, "reference-frame reference")
        _reference(self.metric_model_ref, "metric-model reference")
        _reference(self.local_state_ref, "local-state reference")


@dataclass(frozen=True, slots=True)
class WhenMark:
    """A clock reading situated on a worldline at one spacetime event.

    ``proper_time_seconds`` is the reading carried by the identified atomic/local clock.
    Comparing marks as a tense operation requires a declared comparison rule; marks on
    different worldlines or under different frame/metric models are not silently ordered.
    ``utc_projection`` is optional derived data and never replaces the situated mark.
    """

    clock_ref: str
    worldline_ref: str
    proper_time_seconds: Decimal
    event: SpacetimeEventRef
    uncertainty_seconds: Decimal
    provenance: tuple[str, ...]
    utc_projection: datetime | None = None

    def __post_init__(self) -> None:
        _reference(self.clock_ref, "clock reference")
        _reference(self.worldline_ref, "worldline reference")
        _finite_decimal(self.proper_time_seconds, "proper time", non_negative=True)
        if not isinstance(self.event, SpacetimeEventRef):
            raise TypeError("event must be a SpacetimeEventRef")
        _finite_decimal(self.uncertainty_seconds, "uncertainty", non_negative=True)
        if not self.provenance:
            raise ValueError("WhenMark requires provenance")
        if len(self.provenance) > _MAX_PROVENANCE_ITEMS:
            raise ValueError(
                f"WhenMark provenance exceeds the {_MAX_PROVENANCE_ITEMS}-item limit"
            )
        if len(set(self.provenance)) != len(self.provenance):
            raise ValueError("WhenMark provenance entries must be unique")
        for item in self.provenance:
            _reference(item, "provenance reference")
        if self.utc_projection is not None and self.utc_projection.tzinfo is None:
            raise ValueError("UTC projection must be timezone-aware")

    def to_record(self) -> dict[str, Any]:
        """Return the canonical finite projection used for identity and transport."""

        return {
            "clock_ref": self.clock_ref,
            "event": {
                "frame_ref": self.event.frame_ref,
                "local_state_ref": self.event.local_state_ref,
                "metric_model_ref": self.event.metric_model_ref,
                "position_ref": self.event.position_ref,
                "velocity_ref": self.event.velocity_ref,
            },
            "proper_time_seconds": str(self.proper_time_seconds),
            "provenance": list(self.provenance),
            "schema": "af.ideon.when-mark/v1",
            "uncertainty_seconds": str(self.uncertainty_seconds),
            "utc_projection": (
                self.utc_projection.isoformat() if self.utc_projection is not None else None
            ),
            "worldline_ref": self.worldline_ref,
        }

    def canonical_bytes(self) -> bytes:
        """Return deterministic UTF-8 bytes for the complete mark."""

        return json.dumps(
            self.to_record(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def mark_id(self) -> str:
        """Return the SHA-256 identity of this exact situated mark."""

        return hashlib.sha256(self.canonical_bytes()).hexdigest()
