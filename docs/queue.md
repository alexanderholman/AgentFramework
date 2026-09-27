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
- Reserves due work by descending priority (missing priority means zero), then `available_at`,
  `occurred_at`, and `message_id`. Priorities must fit signed 32-bit integers. Priority never makes a
  future job eligible early.
- Supports per-job delay measured from dispatch time, persisted across worker restarts. Delay and
  priority survive retries and dead-letter inspection. Replay makes a failed job immediately eligible.
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
- Fails closed for unsupported timeout, uniqueness, FIFO-group, and transactional
  dispatch requests.

### `sqs` (protocol compatible; real-service certification pending)

- `SQSQueueDriver` accepts an injected boto3-compatible client with finite timeouts and explicit
  logical lane mappings to standard HTTPS SQS queues and separate failed queues.
- Jobs use the bounded, versioned `QueueJobCodec`. Receipt handles stay private; settlement is bound
  to the owning driver process and current immutable reservation. Renewal replaces that handle.
- Supports delay up to 900 seconds (rounded upward), visibility leases and retry backoff up to 43,200
  seconds, and bounded attempts from SQS `ApproximateReceiveCount`. SQS may refuse renewal beyond its
  maximum remaining visibility period. Such errors are surfaced; workers stop stale settlement.
- Preserves SQS standard-queue at-least-once, best-effort ordering. Release makes a delivery visible
  immediately and consumes a broker receive attempt. It does not decrement SQS's receive counter.
- Exhausted, rejected, malformed, and incorrectly routed messages are copied to the explicit failed
  queue before source deletion. An interrupted transfer can duplicate the failed copy; a failed send
  leaves the source untouched. Failed queues must be monitored and provisioned separately.
- Rejects priority, handler timeout, uniqueness, FIFO, and transactional dispatch requests. No failed
  queue inspection/replay capability is advertised because SQS cannot provide a non-consuming browse.
- `sqs_queue_registration` requires a deployment-supplied capability verifier, so installation alone
  never claims successful service verification. Configure `driver`, `queues`, `dead_letter_queues`,
  and optional `visibility_timeout`; credentials remain in the injected client factory.
- Evidence currently consists of network-free client-protocol tests, not an AWS service run.

API references: [SQS receive](https://docs.aws.amazon.com/boto3/latest/reference/services/sqs/client/receive_message.html),
[SQS visibility](https://docs.aws.amazon.com/boto3/latest/reference/services/sqs/client/change_message_visibility.html),
[SQS send](https://docs.aws.amazon.com/boto3/latest/reference/services/sqs/client/send_message.html).

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

### `beanstalkd` (protocol compatible; real-service certification pending)

- `BeanstalkdQueueDriver` owns an injected Greenstalk-compatible client, configured for binary bodies
  and finite socket timeouts. Lane mappings select exactly one watched tube during each reserve.
- Native priorities are translated so higher AF integers run first. Delay is rounded upward to whole
  seconds. Priority must fit a signed 32-bit integer; delay must fit unsigned 32-bit seconds.
- Reservations use the job's actual server TTR. `touch` renews it and rotates the AF settlement handle.
  Released deliveries consume a server reserve attempt. The server's reserve count bounds retries.
- Rejected, exhausted, malformed, and incorrectly routed jobs are buried in the source tube. Burial
  is a native atomic transition; operators can inspect and recover these jobs with Beanstalkd tools.
- Durability requires server binlog configuration. No durable-service certification is claimed from
  injected-client tests. `beanstalkd_queue_registration` requires a deployment capability verifier.
- Configure `driver`, `tubes`, and optional `reservation_lease`. Supply the SDK's `TimedOutError` in
  `empty_errors` so an empty non-blocking reserve becomes an idle poll. Default payload bound is
  65,535 bytes, matching the server's default maximum job size.
- Handler timeout, uniqueness, FIFO, transactional dispatch and failed-job browsing/replay are not
  advertised. A TTR is a delivery lease, not handler preemption.

References: [Greenstalk API](https://greenstalk.readthedocs.io/en/stable/api.html) and
[Beanstalkd protocol](https://github.com/beanstalkd/beanstalkd/blob/master/doc/protocol.txt).

`QueueJob` can express requested FIFO grouping and transactional dispatch requirements even when the
selected backend does not support them. `QueueManager` validates those requirements as capabilities
before driver execution so unsupported semantics fail closed instead of being silently ignored.

### `rabbitmq` (protocol compatible; real-service certification pending)

- `RabbitMQQueueDriver` owns a Pika-compatible connection created on a dedicated I/O thread. All
  channel operations and heartbeat pumping stay on that thread; configure finite connection,
  socket and blocked-connection timeouts in the injected factory.
- Declares durable quorum source and failed queues, persistent messages, publisher confirms, and
  mandatory routing. The source has a bounded delivery limit and at-least-once dead lettering with
  reject-publish overflow. Existing queues must have compatible arguments; declaration failure is
  surfaced. The broker must support these quorum queue arguments.
- Reservations are manual acknowledgements bound to the owning connection and process. They remain
  unacknowledged until settlement or connection loss; there is no finite AF visibility lease.
- RabbitMQ 4.3 quorum `x-delivery-count` bounds attempts. Retry uses `basic.reject(requeue=True)`;
  release uses `basic.nack(requeue=True)`. On 4.3 release does not increment the failure counter;
  earlier broker versions can count release as a failure. Redelivery without a count fails closed.
- Terminal, malformed and misrouted messages are confirmed into the failed queue before source ack.
  Interrupted transfers can duplicate failed copies. Broker delivery-limit dead lettering also
  catches repeated connection-loss deliveries.
- Delay, delayed backoff, priority, handler timeout, uniqueness, FIFO and transactional dispatch are
  explicitly unsupported. Failed queue browsing and replay are not advertised.
- `rabbitmq_queue_registration` requires independent deployment capability evidence. Configure
  `driver`, `queues`, `dead_letter_queues`, and optional `delivery_limit`. Supply
  `pika.BasicProperties` as the properties factory. Protocol tests do not certify a broker service.

References: [RabbitMQ quorum queues](https://www.rabbitmq.com/docs/quorum-queues) and
[Pika blocking adapter](https://pika.readthedocs.io/en/stable/modules/adapters/blocking.html).

### `redis` (local Redis 6.2.14 wire qualification)

- `RedisQueueDriver` accepts an injected redis-py-compatible client with finite socket timeouts,
  explicit namespace, 1–32 configured lanes, and a positive reservation lease up to one day. Supply
  `conflict_errors=(redis.WatchError,)` to retry optimistic transaction conflicts.
- Jobs, due deliveries, active leases and failed jobs live in a hash and sorted sets. Every mutation
  uses `WATCH`/`MULTI`/`EXEC`; the complete namespace is watched, favoring integrity over throughput
  under contention. Transaction retries are bounded. Keys share a hash tag, but Redis Cluster
  clients and failover have not been qualified.
- Redis `TIME` supplies dispatch, availability and lease timestamps. Higher-priority expired leases
  are recovered before ready jobs; each index orders equal timestamps by message ID. This is
  at-least-once delivery, with no FIFO or job-priority promise.
- Claims persist attempt counters, increasing delivery generations and random process-bound owner
  and reservation tokens. Renewal rotates the token. Exact-expiry, foreign-process and stale handles
  fail without settlement. Redis transactions do not fence a handler's external business effects.
- Delay and retry backoff retain microsecond timestamps. Release returns a job without consuming an
  attempt; retry and lease expiry consume attempts. Exhausted and rejected jobs move atomically to
  failed storage. Bounded inspection and explicit replay preserve the job, reset attempts and retain
  the delivery generation. Duplicate outstanding message IDs are rejected.
- Corrupt records, mismatched indices and wrong Redis key types fail closed and remain available for
  operator diagnosis. The namespace must be exclusively owned by AF, protected by appropriate ACLs,
  and configured without eviction. Persistence and acknowledged-write loss follow Redis AOF/RDB and
  replication settings; they are not guaranteed by an in-memory server test.
- `redis_queue_registration` requires deployment-supplied capability evidence. Configure `driver`,
  `namespace`, optional `lanes`, and optional integer `reservation_lease` seconds. Job priority,
  handler timeout, uniqueness keys, FIFO groups and transactional dispatch remain unsupported.
- `tests/integration/test_live_redis_cache.py` ran against an isolated Redis 6.2.14 TCP server and
  checked 12 competing drivers, delay, lease recovery, renewal, ownership fencing, retry exhaustion,
  failed-job replay and release. The fixture disables persistence; server-restart durability,
  production topology and other Redis versions remain unqualified. Unit protocol tests also exercise
  transaction conflict recovery, corrupt responses and records, and bounded failure handling.

References: [redis-py watched transactions](https://redis.io/docs/latest/develop/clients/redis-py/transpipe/)
and [Redis TIME](https://redis.io/docs/latest/commands/time/).
