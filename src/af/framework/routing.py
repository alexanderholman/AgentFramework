"""Jacob's-ladder routing with an explicit cheap/fast/smart trade-off."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from af.framework.contracts import Procedure
from af.framework.errors import InvalidRoutingPolicy, UnsupportedWork
from af.framework.types import Classification


class OptimisationGoal(StrEnum):
    CHEAPEST = "cheapest"
    FASTEST = "fastest"
    SMARTEST = "smartest"


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    goals: tuple[OptimisationGoal, ...] = (
        OptimisationGoal.CHEAPEST,
        OptimisationGoal.FASTEST,
    )

    def __post_init__(self) -> None:
        if not 1 <= len(self.goals) <= 2 or len(set(self.goals)) != len(self.goals):
            raise InvalidRoutingPolicy("choose one or two distinct routing goals")


class ProcedureRegistry:
    def __init__(self) -> None:
        self._procedures: dict[str, Procedure] = {}

    def register(self, procedure: Procedure) -> None:
        if procedure.id in self._procedures:
            raise ValueError(f"procedure already registered: {procedure.id}")
        self._procedures[procedure.id] = procedure

    def all(self) -> tuple[Procedure, ...]:
        return tuple(self._procedures.values())


class LadderRouter:
    def __init__(self, registry: ProcedureRegistry, policy: RoutingPolicy) -> None:
        self._registry = registry
        self._policy = policy

    def route(self, classification: Classification, *, prompt: str) -> Procedure:
        candidates = [
            procedure for procedure in self._registry.all() if procedure.supports(classification)
        ]
        if not candidates:
            raise UnsupportedWork(prompt)
        return min(candidates, key=self._score)

    def _score(self, procedure: Procedure) -> tuple[int, ...]:
        score = [int(procedure.tier)]
        for goal in self._policy.goals:
            if goal is OptimisationGoal.CHEAPEST:
                score.append(procedure.metrics.cost)
            elif goal is OptimisationGoal.FASTEST:
                score.append(procedure.metrics.latency)
            else:
                score.append(-procedure.metrics.intelligence)
        return tuple(score)
