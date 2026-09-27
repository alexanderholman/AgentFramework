# Configured local self-development

`af self-develop --snapshot snapshot.json` exposes the existing typed development
planner without executing a proposal. The JSON shape is
`DevelopmentPlanningInput` from `af.framework.self_development_execution`.
It contains a `DevelopmentSnapshot`, `SelectionPolicy`, and optional fixed unit.
Caller-supplied snapshots cannot authorize execution.

For the finite local execution path, configure `AF_ENGINEERING_REPOSITORY` and
`AF_SELF_DEVELOPMENT_UNIT`. The latter names a host-owned JSON file validated as
`LocalDevelopmentUnit`: `candidate_id`, `repository` (`owner/name`), exact
`expected_head`, timezone-aware `issued_at` and `expires_at` (at most one hour
apart), and an `EngineeringRequest` containing the bounded `texts` tuple.
This opt-in admits only the existing nonblank-line engineering template described
in [local engineering](local-engineering.md); it is not arbitrary source generation.

Run `af self-develop` to capture current source facts and save an authenticated
plan receipt. Then run `af self-develop --execute --plan-digest sha256:...` with
the returned exact digest. Execution rechecks the current configured source,
principal, tenant, scopes, expiry, cancellation and source HEAD before claiming
the operation and running the existing engineering gates. Planning does not alter
Git; configured planning does write its local durable receipt.

The completion survives application restart. Repeating a completed plan returns
its authenticated receipt, while an interrupted claimed operation requires
recovery and never blindly repeats its effects. A new plan can reuse the admitted
component. This path neither merges the work nor publishes to an external service.
Keep the unit file, database and signing key under host control.

Evidence includes the installed CLI composition, real temporary Git repository,
ten actual engineering gates, restart, direct routing, changed-source rejection,
wrong-principal denial, expiry/cancellation and corrupted ledger tests in
`tests/integration/test_self_development_cycle.py`.
