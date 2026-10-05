"""Minimal prime-set / Grimoire search for Ideon target coverage."""
from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable

@dataclass(frozen=True, slots=True)
class Decomposition:
    primes: frozenset[str]
    operators: frozenset[str] = frozenset()

    @property
    def node_cost(self) -> int:
        return len(self.primes) + len(self.operators)

@dataclass(frozen=True, slots=True)
class Target:
    name: str
    decompositions: tuple[Decomposition, ...]

    @classmethod
    def from_parts(cls, name: str, *parts: Iterable[str]) -> "Target":
        return cls(name, tuple(Decomposition(frozenset(p)) for p in parts))

@dataclass(frozen=True, slots=True)
class MinimalLanguage:
    primes: tuple[str, ...]
    operators: tuple[str, ...]
    target_costs: tuple[tuple[str, int], ...]

    @property
    def vocabulary_size(self) -> int: return len(self.primes)
    @property
    def grammar_size(self) -> int: return len(self.operators)
    @property
    def expression_cost(self) -> int: return sum(c for _, c in self.target_costs)
    def score(self, *, vocabulary_weight: int=1, grammar_weight: int=1, expression_weight: int=1) -> int:
        return vocabulary_weight*self.vocabulary_size + grammar_weight*self.grammar_size + expression_weight*self.expression_cost

def validate_targets(targets: Iterable[Target], candidate_primes: Iterable[str], candidate_operators: Iterable[str]=()) -> None:
    p, o = set(candidate_primes), set(candidate_operators)
    for target in targets:
        if not target.decompositions: raise ValueError(f"{target.name} has no decompositions")
        for d in target.decompositions:
            unknown_p, unknown_o = d.primes-p, d.operators-o
            if unknown_p or unknown_o:
                raise ValueError(f"{target.name} uses undeclared semantics: primes={sorted(unknown_p)}, operators={sorted(unknown_o)}")

def _cost(target: Target, primes: frozenset[str], operators: frozenset[str]) -> int|None:
    viable=[d.node_cost for d in target.decompositions if d.primes<=primes and d.operators<=operators]
    return min(viable) if viable else None

def search_minimal_languages(candidates: Iterable[str], targets: Iterable[Target], *, candidate_operators: Iterable[str]=(), max_primes: int|None=None, max_operators: int|None=None) -> tuple[MinimalLanguage,...]:
    pool=tuple(sorted(set(candidates))); ops=tuple(sorted(set(candidate_operators))); goals=tuple(targets)
    validate_targets(goals,pool,ops)
    plim=len(pool) if max_primes is None else min(max_primes,len(pool)); olim=len(ops) if max_operators is None else min(max_operators,len(ops))
    solutions=[]
    for ps in range(1,plim+1):
      for pc in combinations(pool,ps):
       P=frozenset(pc)
       for os in range(0,olim+1):
        for oc in combinations(ops,os):
         O=frozenset(oc); costs=tuple((g.name,_cost(g,P,O)) for g in goals)
         if any(c is None for _,c in costs): continue
         solutions.append(MinimalLanguage(pc,oc,tuple((n,int(c)) for n,c in costs)))
    frontier=[]
    for item in solutions:
      dominated=any(other.vocabulary_size<=item.vocabulary_size and other.grammar_size<=item.grammar_size and other.expression_cost<=item.expression_cost and (other.vocabulary_size<item.vocabulary_size or other.grammar_size<item.grammar_size or other.expression_cost<item.expression_cost) for other in solutions)
      if not dominated: frontier.append(item)
    return tuple(sorted(frontier,key=lambda x:(x.vocabulary_size,x.grammar_size,x.expression_cost,x.primes,x.operators)))
