# Minimal Prime Language experiment corpus v0

This corpus is an experimental target set for searching a small Principia prime set and
Grimoire. It is not a claim that the listed decompositions are canonical semantics.

## Search model

A target meaning is expressible when at least one declared decomposition can be constructed
from available semantic primes plus permitted Grimoire operators. Search should minimise a
multi-objective cost rather than vocabulary alone:

1. semantic-prime count;
2. Grimoire/operator count;
3. total expression-node cost;
4. maximum expression depth;
5. ambiguity / number of equally valid parses.

Swadesh-style basic vocabulary is a coverage benchmark, not a declaration that each surface
word is semantically prime.

## Candidate semantic primes

ENTITY SELF OTHER PERSON THING LIVING PLANT ANIMAL BODY PART SUBSTANCE WATER FIRE EARTH AIR
SPACE PLACE TIME STATE PROPERTY RELATE CHANGE MOVE CAUSE MAKE USE PERCEIVE KNOW THINK WANT
SAY GIVE TAKE MANY ONE TWO NOT GOOD BAD BIG SMALL SAME DIFFERENT

## Candidate Grimoire operators

COMPOSE ROLE PERSPECTIVE ORDER SCOPE NEGATE QUERY QUANTIFY COMPARE LOCATE TEMPORAL CAUSAL
POSSESS PARTITION TYPE

## Representative decompositions

The machine-readable experiment starts with representative classes before expanding to the
complete Swadesh benchmark:

- I = SELF
- you = OTHER + PERSON
- we = SELF + OTHER + MANY
- person = PERSON
- animal = ANIMAL
- tree = PLANT + BIG
- seed = PLANT + PART
- skin = BODY + PART + OUTSIDE
- blood = BODY + SUBSTANCE + INSIDE
- water = WATER
- fire = FIRE
- earth = EARTH
- sky = SPACE + ABOVE
- sun = THING + LIGHT + ABOVE
- night = TIME + NOT + LIGHT
- eat = LIVING + TAKE + SUBSTANCE + INSIDE
- drink = LIVING + TAKE + WATER + INSIDE
- see = PERCEIVE + LIGHT
- hear = PERCEIVE + SOUND
- know = KNOW
- think = THINK
- say = SAY
- give = CHANGE + POSSESS, with source/destination roles
- receive = CHANGE + POSSESS, with the same graph viewed from destination perspective
- come = MOVE + destination=PERSPECTIVE(SELF)
- go = MOVE + origin=PERSPECTIVE(SELF)
- here = PLACE + PERSPECTIVE(SELF)
- there = PLACE + NOT(here)
- before/after = TIME + ORDER
- in/out = SPACE + LOCATE, reversed containment role
- up/down = SPACE + ORDER relative to a frame
- big/small = PROPERTY + COMPARE
- good/bad = PROPERTY + evaluation frame
- one/two/many = QUANTIFY

The deliberate result is that paired surface concepts such as give/receive, come/go,
in/out, before/after and big/small are candidates for Grimoire-level perspective or ordering,
not automatically separate semantic primes.


## First threshold result: 2026-10-05

The representative executable corpus initially collapses to 18 semantic primes and 12 Grimoire operators:

```text
BODY CHANGE KNOW LIVING MANY MOVE OTHER PERCEIVE PLACE PROPERTY RELATE SAY
SELF SPACE SUBSTANCE THING THINK TIME

COMPARE COMPOSE LOCATE NEGATE ORDER PARTITION PERSPECTIVE POSSESS QUANTIFY
ROLE TEMPORAL TYPE
```

This is **not yet a valid claim of 40-word coverage**. The run exposed a more important modelling problem: a generic operator such as `TYPE` cannot distinguish PERSON, ANIMAL, TREE, WATER, SEE and HEAR unless its type argument carries semantic information. Treating that argument as free would merely hide vocabulary inside grammar.

Therefore every parameter supplied to a Grimoire operator must itself be one of:

1. a registered Ideon expression constructed from the available core;
2. structural information already present in the graph; or
3. explicitly charged extension/domain vocabulary.

The next experiment must distinguish **semantic primes**, **structural Grimoire operators**, and **convenience/extension Ideons**. A small prime core is useful only if extension Ideons have genuine derivations rather than undeclared labels.
