"""Read-only orchestration boundary for AF's governed self-design planner."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from af.framework.self_design import (
    DesignPlan,
    DesignPolicy,
    DesignSnapshot,
    SelfDesignPlanner,
)


class DesignEvidenceProvider(Protocol):
    """Provide one immutable evidence snapshot without granting mutation authority."""

    def capture_snapshot(self) -> DesignSnapshot:
        """Capture the exact evidence used for one bounded planning cycle."""
        ...


@dataclass(frozen=True, slots=True)
class PreparedDesignPlan:
    """Bind one immutable snapshot to the exact pure plan derived from it."""

    snapshot: DesignSnapshot
    plan: DesignPlan

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, DesignSnapshot):
            raise TypeError("prepared self-design snapshot is invalid")
        if not isinstance(self.plan, DesignPlan):
            raise TypeError("prepared self-design plan is invalid")
        if self.plan.snapshot_digest != self.snapshot.digest:
            raise ValueError("prepared self-design plan is not bound to its snapshot")


class SelfDesignOperation:
    """Convert one provider snapshot into one canonical, read-only design plan."""

    __slots__ = ("_clock", "_planner", "_policy", "_provider")

    def __init__(
        self,
        provider: DesignEvidenceProvider,
        policy: DesignPolicy,
        clock: Callable[[], datetime],
        *,
        planner: SelfDesignPlanner | None = None,
    ) -> None:
        if not callable(getattr(provider, "capture_snapshot", None)):
            raise TypeError("self-design evidence provider is invalid")
        if not isinstance(policy, DesignPolicy):
            raise TypeError("self-design policy is invalid")
        if not callable(clock):
            raise TypeError("self-design clock is invalid")
        if planner is not None and not isinstance(planner, SelfDesignPlanner):
            raise TypeError("self-design planner is invalid")
        self._provider = provider
        self._policy = policy
        self._clock = clock
        self._planner = planner or SelfDesignPlanner()

    def prepare(self) -> PreparedDesignPlan:
        """Capture once and bind the snapshot to the exact canonical plan."""
        snapshot = self._provider.capture_snapshot()
        if not isinstance(snapshot, DesignSnapshot):
            raise TypeError("self-design evidence provider returned an invalid snapshot")
        observed_at = self._clock()
        if not isinstance(observed_at, datetime):
            raise TypeError("self-design clock returned an invalid time")
        return PreparedDesignPlan(
            snapshot=snapshot,
            plan=self._planner.plan(snapshot, self._policy, now=observed_at),
        )

    def plan(self) -> DesignPlan:
        """Capture exactly one snapshot and return the planner's canonical result."""
        return self.prepare().plan
