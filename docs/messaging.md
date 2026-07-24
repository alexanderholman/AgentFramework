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

## Deliberate boundaries

The local bus establishes deterministic in-process semantics needed by later outbox, queue, pub-sub, notification, and projection work. Those later systems must preserve their actual delivery guarantees. They must not relabel local dispatch as durable delivery or claim exactly-once transport semantics.
