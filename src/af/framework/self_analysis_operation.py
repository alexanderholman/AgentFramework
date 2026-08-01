"""Read-only orchestration boundary for AF's governed self-analysis planner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from af.framework.self_analysis import (
    AnalysisPlan,
    AnalysisPolicy,
    AnalysisSnapshot,
    SelfAnalysisPlanner,
)


class AnalysisEvidenceProvider(Protocol):
    """Provide one immutable analysis snapshot without granting mutation authority."""

    def capture_snapshot(self) -> AnalysisSnapshot:
        """Capture the exact evidence used for one bounded analysis cycle."""
        ...


@dataclass(frozen=True, slots=True)
class PreparedAnalysisPlan:
    """Bind one immutable analysis snapshot to the exact pure plan derived from it."""

    snapshot: AnalysisSnapshot
    plan: AnalysisPlan

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, AnalysisSnapshot):
            raise TypeError("prepared self-analysis snapshot is invalid")
        if not isinstance(self.plan, AnalysisPlan):
            raise TypeError("prepared self-analysis plan is invalid")
        if self.plan.stage_plan.handoff.input_artifact_digest != self.snapshot.digest:
            raise ValueError("prepared self-analysis plan is not bound to its snapshot")


class SelfAnalysisOperation:
    """Convert one provider snapshot into one canonical, read-only analysis plan."""

    __slots__ = ("_clock", "_planner", "_policy", "_provider")

    def __init__(
        self,
        provider: AnalysisEvidenceProvider,
        policy: AnalysisPolicy,
        clock: Callable[[], datetime],
        *,
        planner: SelfAnalysisPlanner | None = None,
    ) -> None:
        if not callable(getattr(provider, "capture_snapshot", None)):
            raise TypeError("self-analysis evidence provider is invalid")
        if not isinstance(policy, AnalysisPolicy):
            raise TypeError("self-analysis policy is invalid")
        if not callable(clock):
            raise TypeError("self-analysis clock is invalid")
        if planner is not None and not isinstance(planner, SelfAnalysisPlanner):
            raise TypeError("self-analysis planner is invalid")
        self._provider = provider
        self._policy = policy
        self._clock = clock
        self._planner = planner or SelfAnalysisPlanner()

    def prepare(self) -> PreparedAnalysisPlan:
        """Capture once and bind the snapshot to the exact canonical plan."""
        snapshot = self._provider.capture_snapshot()
        if not isinstance(snapshot, AnalysisSnapshot):
            raise TypeError("self-analysis evidence provider returned an invalid snapshot")
        observed_at = self._clock()
        if not isinstance(observed_at, datetime):
            raise TypeError("self-analysis clock returned an invalid time")
        return PreparedAnalysisPlan(
            snapshot=snapshot,
            plan=self._planner.plan(snapshot, self._policy, now=observed_at),
        )

    def plan(self) -> AnalysisPlan:
        """Capture exactly one snapshot and return the planner's canonical result."""
        return self.prepare().plan
