"""Read-only orchestration boundary for AF's governed self-alignment planner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from af.framework.self_alignment import (
    AlignmentPlan,
    AlignmentPolicy,
    AlignmentSnapshot,
    SelfAlignmentPlanner,
)


class AlignmentEvidenceProvider(Protocol):
    """Provide one immutable alignment snapshot without granting mutation authority."""

    def capture_snapshot(self) -> AlignmentSnapshot:
        """Capture the exact evidence used for one bounded alignment cycle."""
        ...


@dataclass(frozen=True, slots=True)
class PreparedAlignmentPlan:
    """Bind one immutable alignment snapshot to the exact pure plan derived from it."""

    snapshot: AlignmentSnapshot
    plan: AlignmentPlan

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, AlignmentSnapshot):
            raise TypeError("prepared self-alignment snapshot is invalid")
        if not isinstance(self.plan, AlignmentPlan):
            raise TypeError("prepared self-alignment plan is invalid")
        if self.plan.stage_plan.handoff.input_artifact_digest != self.snapshot.digest:
            raise ValueError("prepared self-alignment plan is not bound to its snapshot")


class SelfAlignmentOperation:
    """Convert one provider snapshot into one canonical, read-only alignment plan."""

    __slots__ = ("_clock", "_planner", "_policy", "_provider")

    def __init__(
        self,
        provider: AlignmentEvidenceProvider,
        policy: AlignmentPolicy,
        clock: Callable[[], datetime],
        *,
        planner: SelfAlignmentPlanner | None = None,
    ) -> None:
        if not callable(getattr(provider, "capture_snapshot", None)):
            raise TypeError("self-alignment evidence provider is invalid")
        if not isinstance(policy, AlignmentPolicy):
            raise TypeError("self-alignment policy is invalid")
        if not callable(clock):
            raise TypeError("self-alignment clock is invalid")
        if planner is not None and not isinstance(planner, SelfAlignmentPlanner):
            raise TypeError("self-alignment planner is invalid")
        self._provider = provider
        self._policy = policy
        self._clock = clock
        self._planner = planner or SelfAlignmentPlanner()

    def prepare(self) -> PreparedAlignmentPlan:
        """Capture once and bind the snapshot to the exact canonical plan."""
        snapshot = self._provider.capture_snapshot()
        if not isinstance(snapshot, AlignmentSnapshot):
            raise TypeError("self-alignment evidence provider returned an invalid snapshot")
        observed_at = self._clock()
        if not isinstance(observed_at, datetime):
            raise TypeError("self-alignment clock returned an invalid time")
        return PreparedAlignmentPlan(
            snapshot=snapshot,
            plan=self._planner.plan(snapshot, self._policy, now=observed_at),
        )

    def plan(self) -> AlignmentPlan:
        """Capture exactly one snapshot and return the planner's canonical result."""
        return self.prepare().plan
