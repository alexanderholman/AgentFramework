# Durable bounded human decisions

`resolve request` pauses a supported operation without executing it. The finite
reference forms are `count characters in "text"` (choose the character) and
`confirm: <installed deterministic request>` (confirm that exact operation).
Choices come from the installed registry, not caller-supplied procedure IDs or
code. Only pure, replay-safe, read-only procedures with no elevated risk are
eligible. A human response narrows an existing request; it grants no new scope.

```sh
af resolve request 'count characters in "strawberry"' --output decision.json
af resolve inspect DECISION_ID
af resolve respond --decision decision.json --choice CHOICE_ID
af resolve revoke DECISION_ID
```

The private decision file contains the one-use response secret and is created
with owner-only permissions. Standard output omits the secret. The database stores
only its digest, the request digest, pinned procedure descriptors and arguments,
expiry, policy identity, and exact principal/tenant/effective-scope identity.
Keep the host authentication key with authorized state backups.

The same service is available through REST:

- `POST /api/v1/decisions` with `prompt` and optional `expires_in` (1–3600 seconds).
- `GET /api/v1/decisions/{decision_id}` to inspect an authorized pending decision.
- `POST /api/v1/decisions/respond` with `decision_id`, `response_secret`, and `choice_id`.
- `POST /api/v1/decisions/{decision_id}/revoke` to revoke it.

MCP exposes `af_request_human_decision`, `af_get_human_decision`,
`af_respond_human_decision` and `af_revoke_human_decision`. The async tools capture
the trusted invocation context before entering a worker thread and establish a
separate message lease for that explicit handoff. Client parameters cannot replace
identity, tenant, acceptance scope, or policy. MCP network exposure remains
subject to the host transport security boundary.

Responses recheck authority, expiry, policy and the exact procedure descriptor.
A durable atomic claim precedes execution. Execution binds the checked context
with its deadline narrowed to the earlier of the invocation deadline and decision
expiry. Expiry is checked again after claim and after execution; an expired or
unverified outcome is not reported as completed. Crashes and inconclusive results
cannot reuse the response. Create a new decision after investigating the failure.

Every state transition authenticates both the current row and its latest
append-only history entry. Restoring an old signed pending row cannot resurrect a
completed, revoked or claimed response. Tampering, wrong keys, missing history,
changed procedure descriptors, and conflicting responses fail closed. Only one
concurrent response can claim the pending decision. Legacy pending rows without
history remain unavailable and require a new request; initialization does not
silently authorize them.

The reference implementation supports this bounded local clarification and
confirmation journey. It does not infer arbitrary user intent or substitute for
an approval system for privileged external actions.
