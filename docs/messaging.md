# Messaging contracts

AF messaging separates inert process-boundary data from in-process event execution.

## Versioned envelopes

`MessageEnvelope` carries immutable metadata and a recursively bounded JSON payload. The JSON codec accepts only canonical UTF-8 JSON, rejects duplicate or unknown fields, applies explicit byte and payload budgets, and performs no imports, handler lookup, class construction, or publication.

The envelope kind remains material. Domain events, integration events, commands, jobs, broadcasts, and notification intents are not interchangeable merely because they share metadata fields.

## Schema registry and upcasting

`MessageSchemaRegistry` is the application-owned authority for message type, role, and supported schema interval.

Providers declare exact `MessageSchema` values and synchronous one-step upcasters before the registry is frozen. Every supported historical version must have a contiguous path to the current version. Missing steps, duplicate declarations, unknown types, future/retired versions, and kind mismatches fail closed.

An upcaster receives a detached mutable copy of the payload. Its result is snapshotted again under the registry's finite `PayloadLimits` before the next step can run. Upcasters cannot perform asynchronous work, dispatch messages, select handlers, or change metadata other than the next schema version. Failure is sanitized and the original envelope remains unchanged.

Upcasting changes representation, not historical fact. Durable stores should retain the original encoded envelope and record any normalized projection separately when provenance requires both.

## Local event bus

`LocalEventBus` is an in-process-only fan-out boundary for registered domain and integration events. It is not a queue, broker, durable stream, outbox relay, broadcast transport, or cross-process publication mechanism.

Subscriptions preserve declaration order and are frozen before publication. Each event is normalized to the current registered schema before any listener runs. Synchronous publication rejects a subscription set containing asynchronous listeners before invoking the first listener. Asynchronous publication accepts synchronous and asynchronous listeners but still invokes them sequentially in declaration order.

Listeners observe the same immutable normalized envelope and must return `None`. The first failure or cancellation stops later listeners. Cancellation is preserved; ordinary listener failures are projected through a sanitized `LocalEventListenerError`. The returned dispatch report contains only message type, current schema version, and listener count—not payload content.

No-listener publication is a successful zero-listener local dispatch. It does not prove external publication, durable delivery, acknowledgement, or eventual handling.

## Local event middleware

Middleware is declared when the local bus is built and is immutable with the bus. Each middleware extension implements synchronous and asynchronous around-dispatch methods. Middleware observes the same normalized immutable envelope as listeners and must invoke the next stage exactly once. It cannot replace the envelope, skip registered listeners, invoke the chain twice, or alter count-only dispatch evidence.

Synchronous publication performs asynchronous-listener preflight before middleware runs, so an invalid sync dispatch causes no middleware or listener side effects. Cancellation remains authoritative. Ordinary middleware failures are projected through `LocalEventMiddlewareError` without exposing payload or extension state.

`LocalEventTelemetryMiddleware` emits bounded start/completion/failure/cancellation signals through `TelemetryPublisher`. It propagates only the envelope's validated `TelemetryContext` and low-cardinality message role, type, schema version, dispatch mode, stage, and listener count. It never emits payloads, message IDs, correlation values, tenant identities, or listener exceptions. Telemetry creation and sink failure are optional evidence failures and cannot change local dispatch results.

## Transactional outbox storage

`TransactionalOutbox` persists the original canonical bytes of an integration-event envelope through a caller-owned relational transaction. The authoritative state write and outbox append therefore commit or roll back together. The fixed schema records deterministic availability order, bounded attempt state, publication state, and terminal dead-letter state without executing handlers or contacting a transport.

Pending reads return only due, unpublished, non-dead-lettered records ordered by availability time, occurrence time, then message ID. Stored metadata is checked against the decoded canonical envelope; divergence fails closed as an integrity error. Publication and terminal dead-letter transitions are state-idempotent. Retry transitions record one explicit failed attempt and a new availability time.

The storage contract alone is not a relay, lease manager, broker acknowledgement, inbox, deduplication store, or projection checkpoint. It does not claim exactly-once execution or delivery.

## Durable outbox relay

`OutboxRelay` composes `TransactionalOutbox` with an explicit publisher, finite retry policy, and renewable database-backed ownership lease. A logical relay lane has at most one lease generation at a time. Another worker may reclaim the lane only after the recorded expiry. Each acquisition and renewal advances a monotonic fencing generation, so a stale process cannot regain authority merely because a later worker reused the same configured owner identifier. The current generation must be renewed before publication and again before recording the publication result; a stale owner cannot acknowledge, retry, dead-letter, or release a newer lease.

Each invocation selects one bounded due batch in the outbox's deterministic order. A successful publisher return is the transport's acknowledgement boundary and is followed by a durable published transition. A typed retryable `OutboxPublishError` records a stable error code and a bounded next-availability time. A non-retryable failure, or a failure reaching the configured maximum attempt count, records one terminal dead-letter transition. Unexpected publisher exceptions are reduced to the generic `publisher.failure` code, retain their protected cause only for trusted evidence, schedule the durable failure outcome, and surface a sanitized relay error.

The relay deliberately provides **at-least-once publication**, not exactly-once delivery. A process or database failure after the transport accepts an envelope but before the outbox transition commits leaves that envelope pending and it may be published again. Consumers that require side-effect safety must use consumer-scoped inbox/deduplication and projection checkpoints in the same authoritative transaction as their state change. Transport adapters must document whether a normal return truly means accepted, durably accepted, or merely buffered; the relay does not strengthen the underlying transport guarantee.

The initial implementation serializes one logical lane under a global renewable lease. It does not claim parallel per-record leasing, broker transactions, distributed consensus, or exactly-once transport. Those optimizations may be added only with equivalent ownership, fencing, stale-writer, crash-recovery, and real-service evidence.

## Transactional inbox and projection checkpoints

`TransactionalInbox` records consumer-, source-, and message-scoped delivery evidence inside a caller-owned relational transaction. `ProjectionCheckpointStore` retains the latest committed source position for each projection and source. Stored identifiers, positions, and canonical UTC timestamps are validated again when read; corruption or key divergence fails closed without exposing message identities.

`TransactionalProjectionGuard.prepare()` combines both stores. The first accepted source position establishes a baseline. Later accepted positions must be contiguous. The guard returns `APPLY`, `DUPLICATE`, or `STALE`; gaps and conflicting reuse of a source position or message identity fail closed. A stale unseen delivery is recorded without regressing the checkpoint, while a duplicate performs no projection work.

The caller must invoke the guard and apply projection mutations in the same transaction, and must mutate projection state only for `APPLY`. Commit then persists the inbox record, checkpoint, and projection state atomically; any handler or database failure rolls all three back. Concurrent duplicate attempts remain safe because the inbox key and optimistic checkpoint transition allow at most one transaction to commit. This is idempotent projection processing, not an exactly-once transport claim.

Source positions are meaningful only within a stable versioned source or partition identity. A reset, repartition, or incompatible source epoch requires a new `source_name`; silently reusing an existing source identity would make ordering evidence ambiguous.

## Deliberate boundaries

The local bus establishes deterministic in-process semantics needed by later outbox, queue, pub-sub, notification, and projection work. Those later systems must preserve their actual delivery guarantees. They must not relabel local dispatch as durable delivery or claim exactly-once transport semantics.
