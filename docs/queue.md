# Queue contracts

AF queue drivers expose one shared `QueueJob` contract and preserve each backend's real delivery
semantics instead of pretending all brokers are interchangeable.

## Delivery baseline

Every durable AF queue is **at-least-once** unless a specific driver explicitly documents a weaker
mode. AF does not claim exactly-once delivery or side effects.

Observable work must be dispatched only after the authoritative transaction commits. Process-local
`dispatch_after_commit()` is a convenience hook for the current process only; durable cross-process
publication still requires a durable driver and worker boundary.

## Current built-in drivers

### `sync`

- Executes accepted jobs inline in the caller process.
- Preserves declaration order only within one caller's direct dispatch sequence.
- Provides no worker isolation, durability, reservation, retry, delay, FIFO, uniqueness, timeout,
  or transactional broker semantics.
- Fails closed for unsupported delay, priority, retry, timeout, uniqueness, FIFO-group, and
  transactional-dispatch requests.

### `database`

- Persists jobs durably in one relational table owned by the selected database driver.
- Reserves due work in deterministic order: `available_at`, then `occurred_at`, then `message_id`.
- Supports reservation, acknowledgement, bounded retry, lane isolation, restart persistence, and
  terminal dead-letter transitions after the configured attempt bound.
- Uses finite database reservation leases. An abandoned delivery becomes reclaimable at its expiry,
  consumes that delivery attempt, and receives a strictly newer settlement fencing token.
- Rejects acknowledgement, retry, rejection, or release once the reservation lease expires. Queue
  settlement fencing does not stop a stale handler's external side effects; handlers remain
  responsible for idempotent or transactionally fenced business operations.
- Binds each claim to both an opaque driver-process owner and a one-acquisition capability. Wrong
  named connections, reconstructed drivers, fork-inherited drivers, and delete/redispatch ABA handles
  cannot settle the current row even when its public timestamps and generation happen to match.
- Supports explicit renewal of a live reservation. Renewal extends from the trusted current clock,
  rotates the one-acquisition capability, and returns a new immutable settlement handle; the old
  handle is immediately stale. A clock reading that would not strictly advance the stored deadline,
  exact-expiry calls, stale handles, wrong owners, and wrong named connections fail closed without
  extending the lease. The worker kernel renews finite leases automatically while a synchronous
  handler remains active.
- Treats the configured timezone-aware clock as an operational trust boundary. Deployments must keep
  worker clocks synchronized and choose a lease longer than their bounded clock skew; a fast clock can
  cause early redelivery, but the newer generation still fences stale queue settlement. Existing
  reserved rows from older schemas use `reserved_at + reservation lease` as their finite compatibility
  deadline.
- Requires a drain-first upgrade from binaries that predate owner-bound settlement. Old workers ignore
  the additive ownership columns and must not overlap new workers; rollback likewise requires stopping
  workers and waiting at least the maximum reservation lease. Ownership tokens are opaque database
  state and are redacted from reservation representations, errors, and telemetry.
- Fails closed for unsupported delay, priority, timeout, uniqueness, FIFO-group, and transactional
  dispatch requests.

Worker-managed renewal runs independently while a synchronous handler is active, serializes renewal
with terminal settlement, and always settles with the latest immutable handle. Renewal loss or an
ambiguous renewal failure suppresses stale settlement but cannot stop handler side effects already in
progress. Queue driver operations, including renewal, remain synchronous trust boundaries and must be
operationally bounded; shutdown timeouts do not interrupt a blocked driver call.

The owned worker runtime's timeout bounds admission waits, worker joins, and coordination between
concurrent shutdown callers. It cannot preempt a synchronous driver operation already executing,
including `reserve`, settlement, renewal, or `close`. Drivers must therefore operationally bound every
synchronous operation themselves. In particular, runtime close invokes driver close in the calling
thread rather than starting unsafe background cleanup that could outlive the runtime's ownership.

Runtime drain stops intake and lets admitted work finish. Runtime close additionally signals one
worker-owned cancellation token to each active handler that implements `CooperativeJobHandler` by
declaring `cooperative_cancellation = True` and providing synchronous
`invoke(job, *, cancellation) -> None`.
Container-resolved handlers observe that exact token through their invocation context. A cooperative
return releases the latest renewed reservation only after handler scope disposal; it does not record
idempotency or acknowledge the job. Legacy handlers remain graceful-drain-only. A handler that ignores
cancellation is never released while its side effects may still continue: close can time out in the
draining state, renewal remains active, and a later close can finish after the handler exits. This is
in-process cooperation, not thread preemption, process supervision, or exactly-once execution.

## Capability boundaries

`QueueJob` can express requested FIFO grouping and transactional dispatch requirements even when the
selected backend does not support them. `QueueManager` validates those requirements as capabilities
before driver execution so unsupported semantics fail closed instead of being silently ignored.

Future Redis, RabbitMQ, SQS, and Beanstalkd drivers must document their exact acknowledgement,
ordering, visibility, retry, and dead-letter behavior with real-service conformance evidence before
claiming those capabilities.
