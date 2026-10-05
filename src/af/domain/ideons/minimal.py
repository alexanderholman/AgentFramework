"""Minimal prime-set / Grimoire search for Ideon target coverage.

This module is deliberately domain-generic: callers provide candidate semantic
primes, target decompositions, and a composition-cost model.  It searches for
small prime sets which can express the requested targets and reports the
trade-off between vocabulary size and expression complexity.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Mapping

@dataclass(frozen=True, slots=True)
class Target:
    name: str
    decompositions: tuple[frozenset[str], ...]

    @classmethod
    def from_parts(cls, name: str, *parts: Iterable[str]) -> "Target":
        return cls(name, tuple(frozenset(p) for p in parts))

@dataclass(frozen=True, slots=True)
class MinimalLanguage:
    primes: tuple[str, ...]
    target_costs: tuple[tuple[str, int], ...]

    @property
    def vocabulary_size(self) -> int:
        return len(self.primes)

    @property
    def expression_cost(self) -> int:
        return sum(cost for _, cost in self.target_costs)

    def score(self, *, vocabulary_weight: int = 1, expression_weight: int = 1) -> int:
        return vocabulary_weight * self.vocabulary_size + expression_weight * self.expression_cost

def _cost(target: Target, available: frozenset[str]) -> int | None:
    viable = [len(parts) for parts in target.decompositions if parts <= available]
    return min(viable) if viable else None

def search_minimal_languages(
    candidates: Iterable[str],
    targets: Iterable[Target],
    *,
    max_primes: int | None = None,
) -> tuple[MinimalLanguage, ...]:
    """Return the Pareto frontier for vocabulary size vs composition cost."""
    pool = tuple(sorted(set(candidates)))
    goals = tuple(targets)
    limit = len(pool) if max_primes is None else min(max_primes, len(pool))
    solutions: list[MinimalLanguage] = []
    for size in range(1, limit + 1):
        for choice in combinations(pool, size):
            available = frozenset(choice)
            costs = tuple((goal.name, _cost(goal, available)) for goal in goals)
            if any(cost is None for _, cost in costs):
                continue
            solutions.append(MinimalLanguage(choice, tuple((n, int(c)) for n, c in costs)))
    frontier: list[MinimalLanguage] = []
    for item in solutions:
        dominated = any(
            other.vocabulary_size <= item.vocabulary_size
            and other.expression_cost <= item.expression_cost
            and (other.vocabulary_size < item.vocabulary_size or other.expression_cost < item.expression_cost)
            for other in solutions
        )
        if not dominated:
            frontier.append(item)
    return tuple(sorted(frontier, key=lambda x: (x.vocabulary_size, x.expression_cost, x.primes)))
