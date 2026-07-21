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

Cost minimisation is subordinate to quality and hard requirements. Cost evidence includes
latency, compute and energy, money, privacy exposure, and operational risk. Quality evidence
includes accuracy, availability, provenance, and verified execution history.

## Initial scopes

- `child`: levels 0–2 only; deterministic procedural responses; no generative AI
- `deterministic`: levels 0–2 unless explicitly changed
- `reviewed-generative`: levels 3–5 only when lower levels are insufficient; generated
  answers are withheld pending review
- `operator-evaluation`: explicit testing and benchmarking without silent production use

## Planning and generation

A model-backed planner may propose only a typed DAG of registered capabilities. The runtime
validates nodes, edges, arguments, dependencies, requirements, scope, and policy before any
execution. Generated answers are plausible but rare and never bypass review.

## Reuse

Review and caching are separate gates. A result may be correct but still `no-store` because
it is personal, sensitive, short-lived, non-replay-safe, or forbidden by scope. Reuse
requires valid provenance, fingerprints, review evidence, expiry, and independent
verification.
