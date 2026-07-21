# Capability Ladder and Acceptance Scopes

AF asks:

> What is the cheapest verified mechanism that can satisfy this intent at the required
> quality and within the request's acceptance scope?

Algorithms are functions. Levels distinguish simple functions from increasingly complex
systems of functions:

| Level | Capability class | Examples |
|---:|---|---|
| 0 | Cached verified reuse | exact result cache, compiled reusable result |
| 1 | Simple deterministic function | character counting, direct transformation |
| 2 | Complex deterministic function system | database query, FTS, PageRank, simulation, deterministic DAG |
| 3 | Specialist model | embedding model, narrow scientific ML model |
| 4 | Adaptive agent workflow | validated DAG of registered capabilities with adaptive control |
| 5 | Large reasoning or generative model | reviewed answer generation or complex reasoning |
| 6 | Human or external expert | clarification, approval, specialist judgement |

Selection is lexicographic: choose the lowest capability level satisfying quality,
requirements, availability, and effective scope; then minimize cost within that level. Cost
evidence includes latency, compute and energy, money, privacy exposure, and operational
risk. Quality evidence includes accuracy, availability, provenance, and verified execution
history.

## Initial scopes

- `child`: automated levels 0–2 only; deterministic procedural responses; no generative AI
  or model-derived cache; same-audience child-eligible reuse only
- `deterministic`: automated levels 0–2; deterministic provenance and reuse only
- `reviewed-generative`: levels 3–5 only when lower levels are insufficient; generated
  answers are withheld pending review
- `operator-evaluation`: explicit levels 0–5 testing, no production release, and `no-store`
  by default

Effective scope is the intersection of caller preference and trusted identity, session,
tenant, parental, and delegation policy. A caller or child context cannot self-select a more
permissive scope. Every scope can return a non-automated level-6 human clarification or
handoff without enabling a higher automated level.

## Planning and generation

A model-backed planner may propose only a typed DAG of registered capabilities. The runtime
validates nodes, edges, arguments, dependencies, requirements, scope, and policy before any
execution. Model-derived taint propagates through planner arguments, deterministic wrappers,
and all derivatives. Generated answers are plausible but rare and never bypass review.

Planner execution is pure, read-only, and no-risk by default. Side-effecting, risky,
irreversible, externally visible, or confirmation-requiring nodes need independent
pre-execution authorization bound to the exact immutable plan, arguments, predicted effects,
scope, policy version, expiry, and authorizer. Any adaptive change invalidates that
authorization and pauses before the changed effect.

## Reuse

Review and caching are separate gates. A result may be correct but still `no-store` because
it is personal, sensitive, short-lived, non-replay-safe, or forbidden by scope. Reuse
requires valid provenance, origin level, derivation taint, fingerprints, review evidence,
expiry, audience/tenant eligibility, and independent verification. Review evidence binds to
the exact output digest, derivation graph, effective scope, authorized reviewer, policy
version, expiry, and revocation state.
