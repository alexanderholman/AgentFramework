"""Executable threshold experiment for a small Prime Language core.

The decompositions are hypotheses, not canonical definitions.  The purpose is to make
assumptions explicit and measurable so later runs can replace them.
"""
from af.domain.ideons.minimal import Decomposition, Target, search_minimal_languages

P=("SELF","OTHER","THING","LIVING","BODY","SUBSTANCE","SPACE","PLACE","TIME","PROPERTY","RELATE","CHANGE","MOVE","PERCEIVE","KNOW","THINK","SAY","MANY","NOT")
G=("COMPOSE","ROLE","PERSPECTIVE","ORDER","NEGATE","QUERY","QUANTIFY","COMPARE","LOCATE","TEMPORAL","CAUSAL","POSSESS","PARTITION","TYPE")

def d(primes=(),ops=()): return Decomposition(frozenset(primes),frozenset(ops))

TARGETS=(
 Target("I",(d(("SELF",)),)), Target("you",(d(("OTHER",),("TYPE",)),)),
 Target("we",(d(("SELF","OTHER","MANY"),("COMPOSE",)),)),
 Target("person",(d(("LIVING","THING"),("TYPE",)),)),
 Target("animal",(d(("LIVING","THING"),("TYPE",)),)),
 Target("tree",(d(("LIVING","THING"),("TYPE",)),)),
 Target("seed",(d(("LIVING","THING"),("PARTITION","TYPE")),)),
 Target("skin",(d(("BODY",),("PARTITION","LOCATE")),)),
 Target("blood",(d(("BODY","SUBSTANCE"),("PARTITION","LOCATE")),)),
 Target("water",(d(("SUBSTANCE",),("TYPE",)),)),
 Target("fire",(d(("SUBSTANCE","CHANGE"),("TYPE",)),)),
 Target("earth",(d(("SUBSTANCE",),("TYPE",)),)),
 Target("sky",(d(("SPACE",),("LOCATE",)),)),
 Target("sun",(d(("THING",),("TYPE","LOCATE")),)),
 Target("night",(d(("TIME",),("TEMPORAL","NEGATE")),)),
 Target("eat",(d(("LIVING","CHANGE","SUBSTANCE"),("ROLE","LOCATE")),)),
 Target("drink",(d(("LIVING","CHANGE","SUBSTANCE"),("ROLE","LOCATE","TYPE")),)),
 Target("see",(d(("PERCEIVE",),("TYPE",)),)), Target("hear",(d(("PERCEIVE",),("TYPE",)),)),
 Target("know",(d(("KNOW",)),)), Target("think",(d(("THINK",)),)), Target("say",(d(("SAY",)),)),
 Target("give",(d(("CHANGE",),("POSSESS","ROLE","PERSPECTIVE")),)),
 Target("receive",(d(("CHANGE",),("POSSESS","ROLE","PERSPECTIVE")),)),
 Target("come",(d(("MOVE",),("ROLE","PERSPECTIVE")),)), Target("go",(d(("MOVE",),("ROLE","PERSPECTIVE")),)),
 Target("here",(d(("PLACE","SELF"),("PERSPECTIVE",)),)), Target("there",(d(("PLACE","SELF"),("PERSPECTIVE","NEGATE")),)),
 Target("before",(d(("TIME",),("ORDER",)),)), Target("after",(d(("TIME",),("ORDER","PERSPECTIVE")),)),
 Target("in",(d(("SPACE",),("LOCATE","ROLE")),)), Target("out",(d(("SPACE",),("LOCATE","ROLE","PERSPECTIVE")),)),
 Target("big",(d(("PROPERTY",),("COMPARE",)),)), Target("small",(d(("PROPERTY",),("COMPARE","PERSPECTIVE")),)),
 Target("one",(d((),("QUANTIFY",)),)), Target("two",(d((),("QUANTIFY",)),)), Target("many",(d(("MANY",),("QUANTIFY",)),)),
 Target("not",(d(("NOT",)), d((),("NEGATE",)))),
 Target("same",(d(("RELATE",),("COMPARE",)),)), Target("different",(d(("RELATE",),("COMPARE","NEGATE")),)),
)

def run():
    # This corpus currently requires every listed semantic prime except OTHER can be
    # represented by SELF + PERSPECTIVE only after identity semantics are formally
    # declared; we therefore do not permit that collapse yet.
    result=search_minimal_languages(P,TARGETS,candidate_operators=G,max_primes=len(P),max_operators=len(G))
    return result
