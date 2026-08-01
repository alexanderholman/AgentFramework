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
- Fails closed for unsupported delay, priority, timeout, uniqueness, FIFO-group, and transactional
  dispatch requests.

## Capability boundaries

`QueueJob` can express requested FIFO grouping and transactional dispatch requirements even when the
selected backend does not support them. `QueueManager` validates those requirements as capabilities
before driver execution so unsupported semantics fail closed instead of being silently ignored.

Future Redis, RabbitMQ, SQS, and Beanstalkd drivers must document their exact acknowledgement,
ordering, visibility, retry, and dead-letter behavior with real-service conformance evidence before
claiming those capabilities.
