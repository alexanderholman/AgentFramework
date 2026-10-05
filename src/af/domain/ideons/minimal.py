"""Minimal prime-set / Grimoire search for Ideon target coverage."""
from __future__ import annotations
from dataclasses import dataclass
from itertools import combinations, product
from typing import Iterable

@dataclass(frozen=True, slots=True)
class Decomposition:
    primes: frozenset[str]
    operators: frozenset[str]=frozenset()
    @property
    def node_cost(self)->int: return len(self.primes)+len(self.operators)

@dataclass(frozen=True, slots=True)
class Target:
    name:str
    decompositions:tuple[Decomposition,...]
    @classmethod
    def from_parts(cls,name:str,*parts:Iterable[str])->"Target":
        return cls(name,tuple(Decomposition(frozenset(p)) for p in parts))

@dataclass(frozen=True, slots=True)
class MinimalLanguage:
    primes:tuple[str,...]; operators:tuple[str,...]; target_costs:tuple[tuple[str,int],...]
    @property
    def vocabulary_size(self)->int:return len(self.primes)
    @property
    def grammar_size(self)->int:return len(self.operators)
    @property
    def expression_cost(self)->int:return sum(c for _,c in self.target_costs)
    def score(self,*,vocabulary_weight:int=1,grammar_weight:int=1,expression_weight:int=1)->int:
        return vocabulary_weight*self.vocabulary_size+grammar_weight*self.grammar_size+expression_weight*self.expression_cost

def validate_targets(targets:Iterable[Target],candidate_primes:Iterable[str],candidate_operators:Iterable[str]=())->None:
    p,o=set(candidate_primes),set(candidate_operators)
    for t in targets:
        if not t.decompositions: raise ValueError(f"{t.name} has no decompositions")
        for d in t.decompositions:
            up,uo=d.primes-p,d.operators-o
            if up or uo: raise ValueError(f"{t.name} uses undeclared semantics: primes={sorted(up)}, operators={sorted(uo)}")

def _cost(t:Target,p:frozenset[str],o:frozenset[str])->int|None:
    v=[d.node_cost for d in t.decompositions if d.primes<=p and d.operators<=o]
    return min(v) if v else None

def _pareto(items:list[MinimalLanguage])->tuple[MinimalLanguage,...]:
    out=[]
    for x in items:
        if not any(y.vocabulary_size<=x.vocabulary_size and y.grammar_size<=x.grammar_size and y.expression_cost<=x.expression_cost and (y.vocabulary_size<x.vocabulary_size or y.grammar_size<x.grammar_size or y.expression_cost<x.expression_cost) for y in items): out.append(x)
    return tuple(sorted(set(out),key=lambda x:(x.vocabulary_size,x.grammar_size,x.expression_cost,x.primes,x.operators)))

def search_minimal_languages(candidates:Iterable[str],targets:Iterable[Target],*,candidate_operators:Iterable[str]=(),max_primes:int|None=None,max_operators:int|None=None)->tuple[MinimalLanguage,...]:
    """Exact search by choosing one decomposition per target.

    This avoids enumerating the full powersets of primes/operators: every minimal
    feasible language is the union of at least one decomposition choice per target.
    """
    pool=set(candidates); ops=set(candidate_operators); goals=tuple(targets)
    validate_targets(goals,pool,ops)
    # Deduplicate equivalent union states incrementally.
    states={(frozenset(),frozenset())}
    for goal in goals:
        states={(p|d.primes,o|d.operators) for p,o in states for d in goal.decompositions}
        # Drop states already dominated on set inclusion; supersets cannot later win.
        kept=set(states)
        for a in states:
            if any(b!=a and b[0]<=a[0] and b[1]<=a[1] for b in states): kept.discard(a)
        states=kept
    solutions=[]
    for p,o in states:
        if max_primes is not None and len(p)>max_primes: continue
        if max_operators is not None and len(o)>max_operators: continue
        costs=tuple((g.name,_cost(g,p,o)) for g in goals)
        if any(c is None for _,c in costs): continue
        solutions.append(MinimalLanguage(tuple(sorted(p)),tuple(sorted(o)),tuple((n,int(c)) for n,c in costs)))
    return _pareto(solutions)
