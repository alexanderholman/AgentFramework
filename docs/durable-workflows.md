# Durable installed workflows

AF's shared `WorkflowService` runs bounded dependency DAGs through installed,
digest-pinned components. The reference `text.count-total` composition applies
`CharacterCountProcedure` to a batch, verifies every count independently, then
sums the verified vector with an independent aggregate check.

```bash
af workflows start '{"texts":["strawberry","raspberry"],"character":"r"}' --max-nodes 1
af workflows resume RUN_ID
af workflows cancel RUN_ID
```

The first command intentionally stops after one verified stage. A new application
process can resume the second stage from the same SQLite database and host key.
Repeating a completed run returns its checkpoint without executing its components.

Checkpoint rows and append-only revisions are authenticated with a separate local
host key. They bind the complete definition, component digests, input/output
hashes, verifier evidence, original authority and acceptance scope, policy
version, deadlines, node attempts, and fencing leases. A completed status without
valid output evidence cannot admit a dependent. Restoring an old current row
without its matching latest history revision is rejected. Protection from rolling
back an entire database requires an externally trusted backup/monotonic record.

During execution, a cooperative guard observes persistent cancellation, parent
cancellation, authorization checks, deadlines, and resource lease revocation.
It renews the node fence while execution and verification are active. Installed
handlers must obey their cancellation token and declared replay contract.
Cancellation discards an in-flight result, releases the resource lease on exit,
and records that earlier authenticated checkpoints are retained. Noncooperative
arbitrary Python cannot be forcibly terminated by a thread-level coordinator;
subprocess jobs use AF's bounded process-tree executor instead.

After worker loss, replay-safe stages may retry with the same stable effect key.
Uncertain stages with non-replay-safe effects enter `needs-human`; the coordinator
does not assume that an absent result means an absent external effect. Evidence
invalidation records a new revision and invalidates dependent results while
preserving earlier evidence history.

`WorkflowService.progress()` reports the verified fraction and every node state.
An ETA is an interval estimated from completed-stage durations, with explicit
comparable-cost/resource assumptions, confidence, observation count, update time,
and freshness. Unobserved or blocked estimates are unknown (`None`).

`CompositionPlanner` searches bounded paths through typed installed component
contracts. It checks schema flow, effects, and scopes before creating a DAG.
Missing functionality returns a typed `CompositionGap`. This reference slice
does not generate or admit arbitrary source code; new components require the
separate engineering and independent admission process before installation.
