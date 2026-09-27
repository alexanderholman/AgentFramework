# Bounded self-operation first slices

`SelfImprovementService` composes fixture stage ports through the existing
`SelfOperationStage`, `StageHandoff`, and `StageResult` contracts. It starts at the
earliest gate lacking an exact, current, independently admitted handoff. Each
new stage requires a fresh source snapshot and retains exact snapshot, handoff,
policy, result and parent-operation bindings.

A stage reservation and its completion are separate authenticated append-only
ledger records. Replay skips admitted results. An interrupted reservation becomes
an explicit recovery stop because the wrapper cannot prove that an arbitrary
external stage effect did not happen. Only one active stage can claim an operation.
Deadline, stage-count, cost, source budgets and repeated no-progress limits bound
the lifecycle. Rejections stop downstream dispatch and can identify the earliest
affected upstream stage without recursively retrying it.

`FixtureSelfImprovement` is an executable ten-stage lifecycle demonstrating these
contracts. It produces fixture evidence and cannot mutate repositories, publish,
merge, qualify a release, or grant real operation authority. Real stage adapters
must supply those separately configured authorities and their own admission gates.

`plan_evolution(EvolutionSnapshot)` is a pure planner over admitted, minimized
longitudinal observations. It requires persistent uncertainty-separated change,
three training periods, a later held-out period, a baseline comparison,
counterfactual evidence, reversible change, explicit anti-Goodhart review,
alternatives, guardrails, bounded cost, rollback and expiry. Weak, sensitive,
confounded, overlapping or unadmitted evidence yields a typed zero-effect stop.
Identical canonical evidence produces byte-identical proposal JSON and digests.

An evolution plan is a proposal only. `admitted_evolution_objective()` accepts
only a current, independently admitted exact proposal digest; it does not invoke
a development stage or supply publication authority. Neither first slice collects
telemetry or transfers raw prompts into its evidence ledger.
