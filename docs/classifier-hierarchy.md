# Bounded classifier hierarchy

The installed `ClassifierChain` traverses a typed hierarchy before the existing
`PipelineAgent` performs `route → execute → verify → record → test → reuse`.
`af dev describe --json` exposes node identities, declared versions/catalogue digests,
children, procedure slices, budgets and configured selector metadata without running
classifiers. `What time is it?` traverses root/system/date-time and executes the
registered fresh clock procedure with an explicit UTC offset. It calls no model.

A `ClassifierNode` returns `FunctionRoute`, `ChildRoute`, `NoRoute`,
`NeedsClarification`, `Denied`, or `ClassifierFailure`. Its parent owns ordered sibling
recovery after `NoRoute`; only an exhausted root permits the next configured rung.
Denial, ambiguity, malformed output, cancellation, unavailable classifiers and budget
exhaustion are terminal. Nodes cannot redirect to undeclared children or functions.
Unique ownership and reachable, acyclic topology are checked at construction.

Hosts can inject a `HierarchyRouter` through `create_application` or
`create_application_async` using `classifier_hierarchy=`. Default composition adapts
installed procedure contributions; it retains one procedure registry and independent
verifier. Custom nodes receive a normalized prompt, effective acceptance scope,
immutable attempt history and remaining budgets. Decision context excludes identity,
presentation locale and arbitrary request metadata, as for the existing classifier.
Installed callbacks are trusted Python extensions, not sandboxed arbitrary code.

Statistical callbacks must declare `selector_metadata=SelectorMetadata(...)` in their
host-owned node registration. Metadata never comes from model output. A statistical
ancestor also subjects its descendant's proposal to the existing selector policy:
local selection, sufficient confidence, pure read-only/no-risk procedure, no implicit
confirmation grant, and independent grounding. Remote nodes are denied before the
callback. A typed statistical proposal is neither execution authority nor a verifier.
The policy remains deliberately narrower than the installed deterministic catalogue.
Jev is optional; these contracts have no provider-specific application logic.

Function proposals must match the node's procedure identity, version, digest, argument
schema and confidence policy. The pipeline rechecks the live registry, routed identity,
constitution, arguments, statistical grounding when applicable, and cancellation/deadline
at execution. It records node/function identities, versions/digests, finite reasons,
confidence, elapsed time and rung without retaining prompt or argument values in the
routing trace. Ambiguity stays explicit and never becomes a fabricated answer.

`RouteBudget` bounds total nodes/calls, depth, per-rung nodes, number of rungs, elapsed
time and declared cost units. Those units are host estimates, not metered billing.
Synchronous callbacks must cooperate with cancellation and have their own bounded I/O;
AF cannot forcibly interrupt arbitrary synchronous Python. Late results are rejected.
The existing explicitly configured fallback selector remains available after ordinary
`NoRoute`, using its existing provider timeout, validation and policy. Terminal hierarchy
outcomes never reach it. No fallback provider is enabled by default.

Verification: `tests/unit/test_classification_hierarchy.py` and
`tests/feature/test_classifier_hierarchy.py` cover wrong-child/sibling recovery,
parent exhaustion, one-rung escalation, cycle/duplicate rejection, all terminal
outcomes, budgets, clocks, cancellation, current-catalogue binding, zero implicit
egress, statistical grounding, parent influence and shared installed execution.
These deterministic provider doubles prove routing contracts, not real model readiness.
