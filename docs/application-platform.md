# AF Application Platform Contracts

AF uses Laravel's application, container, service-provider, manager, driver, kernel, and
package vocabulary while remaining Python-native. Protocols, dataclasses, immutable values,
constructor/factory injection, and explicit lifecycle replace static facades, Active Record
coupling, hidden global state, and reflection-heavy autowiring.

This document defines the platform contract established by SPEC-015, DEC-028, and DEC-029.
It does not claim that every planned manager or kernel is implemented yet.

## Application lifecycle

```text
construct
  -> register every provider
  -> validate provider graph and bindings
  -> boot every provider
  -> serve through one or more kernels
  -> stop intake and drain bounded in-flight work
  -> shut down providers and resources in reverse order
```

`register()` is declaration-only: it records bindings, factories, inert metadata, and provider
dependencies without external I/O, service resolution, construction, or local side effects.
After every provider registers, AF topologically validates and freezes the graph. Providers
then boot in dependency order.

`boot()` may open resources, negotiate runtime capabilities, register transport hooks, and
perform bounded I/O. A provider registers each acquired resource with its boot transaction as
it is acquired. If boot fails, AF first cleans the currently booting provider's partial
resources, then every previously booted provider/resource exactly once in reverse order.

Shutdown is idempotent and cancellation-safe. Bounded protected cleanup continues after one
cleanup failure or cancellation, aggregates protected evidence, and re-raises the original
cancellation after reverse-order unwinding.

## Sync, async, and cancellation

AF declares each public contract as synchronous or asynchronous.

- External I/O, brokers, remote databases, streams, workers, boot, and shutdown are
  async-first.
- Existing deterministic procedures and CPU-only functions remain synchronous.
- Explicit adapters invoke synchronous work from an async application, including through a
  bounded executor when needed.
- A process entry point may own one sync-to-async bridge. Libraries never call
  `asyncio.run()` or create nested event loops.
- Cancellation and shutdown propagate. They are not provider errors and are not retried
  unless a higher-level contract explicitly resumes idempotent work.

## Lifetimes and ownership

| Lifetime | Identity and ownership |
|---|---|
| Singleton | One container-wide stateless or concurrency-safe service |
| Application | One instance per booted `Application` |
| Invocation | One instance per request, command, message, job, event, schedule, notification, or TUI interaction turn |
| Transient | A new instance per resolution; owned by the resolving scope when disposable |

Every invocation receives a fresh scope. The scope owns dependencies it creates and disposes
sync/async resources once in reverse creation order. Disposed scopes reject resolution and
context access.

Tasks spawned by an invocation are scope-owned and must complete or be cancelled before the
scope is disposed. Explicit detached work receives a newly derived context, cannot retain
scoped dependencies, and crosses a typed job/event boundary rather than inheriting the
original Python context implicitly.

Immutable context carries principal, tenant, requested/effective acceptance scope,
correlation/causation IDs, trace context, deadline, locale, and cancellation. Long-lived
services do not capture requests, users, tenants, transactions, mutable serializers, or
driver sessions.

## Providers and packages

Providers have deterministic dependencies and lifecycle hooks:

```python
class ServiceProvider(Protocol):
    def register(self, application: Application) -> None: ...
    async def boot(self, application: Application) -> None: ...
    async def shutdown(self, application: Application) -> None: ...
```

Inert package metadata may declare providers, compatibility ranges, dependencies,
configuration, commands, routes, events, migrations, resources, and claimed capabilities.
AF inspects that metadata without importing package/provider modules. Discovery is reviewable,
consumer-disableable, and requires compatibility validation plus explicit policy approval
before import or construction. Packages do not mutate source or execute install actions
implicitly.

Approval, manifests, probes, and conformance evidence are bound to the immutable artifact
digest/version, dependency-graph and configuration digests, deployment profile, policy
version, timestamp, expiry, and revocation state. AF revalidates before import/use whenever
those values or the probed runtime state change.

## Managers, named connections, and drivers

```text
injected contract or ConnectionRef
  -> subsystem Manager
  -> named/default connection configuration
  -> registered DriverFactory
  -> capability-validated Driver
  -> backend client
```

Examples include DatabaseManager, SearchManager, CacheManager, QueueManager, EventBusManager,
FilesystemManager, MailManager, and NotificationManager. Managers cache drivers only at a
declared safe lifetime. Drivers own clients and report health, version, capabilities, and
cleanup behavior.

Configuration does not infer a production backend from installed packages. Unknown drivers,
missing secrets, malformed settings, incompatible versions, and missing capabilities fail
closed. A configured production driver never silently becomes memory, local disk, embedded
search, or synchronous execution.

Capabilities are not trusted merely because a driver reports them. Critical transaction,
tenancy, durability, consistency, lock, replay, and policy capabilities require trusted
static manifest claims plus independent runtime probes and conformance evidence appropriate
to the deployment profile.

Fallback, failover, and load balancing are explicit composite drivers with fixed reason codes
and visible degraded state.

## Capability-specific data contracts

AF shares identity, tenancy, provenance, health, pagination, and policy concepts without
pretending every store has the same query language.

- Relational: transactions, savepoints, repositories, relational queries.
- Document: filters, projections, arrays, aggregations, sessions.
- Graph: nodes, edges, patterns, paths, bounded traversal.
- Search: text, facets, highlights, vectors, hybrid ranking, index lifecycle.
- Cache/lock: eviction-tolerant values and ownership-safe coordination.
- Blob: logical keys, streams, metadata, checksums, visibility, signed URLs.
- Event/time-series: ordering scope, timestamps, retention, replay.

Unsupported operations fail before execution. Native access is explicit, policy-gated, and
observable. ACID claims end at one supporting connection. Cross-store workflows use
outbox/inbox, idempotent projection, or explicit sagas.

An authoritative state change and its outbox record are persisted atomically in the same
local transaction. A post-commit relay publishes the outbox event; idempotent consumers use
inbox/deduplication state where required.

When consumer business state and inbox share a transaction, they commit atomically. External
effects first create a durable transactional intent/outbox; acknowledgement follows durable
intent and the channel's declared completion policy so a crash cannot silently lose or
duplicate an untracked effect.

### Unit of work and after-commit callbacks

`DatabaseManager.unit_of_work()` selects the same named, read/write-routed,
capability-verified driver as `transaction()` and returns a single-use transaction boundary.
It carries a finite ordered set of process-local callbacks. Successful commit runs all
registered callbacks in order and reports their sanitized aggregate failures; rollback and
commit failure run none. Registration beyond the configured finite bound fails before another
callback is retained.

After-commit callbacks are deliberately non-durable. Cross-process publication, external
effects, retries, and work that must survive a crash use the transactional outbox. A callback
failure after commit does not imply that the authoritative database transaction was reversed.

## Delivery semantics

| Mechanism | Meaning | Baseline guarantee |
|---|---|---|
| Command/job | Request that work be performed | At-least-once handling unless explicitly weaker |
| Domain/application event | Fact inside one application boundary | Sync or transaction-deferred as declared |
| Integration event | Versioned fact for external consumers | Durable only with outbox and durable transport |
| Broker pub-sub | Fan-out fact to subscriptions | Driver-specific; ephemeral modes are lossy |
| Broadcast | Client-facing realtime projection | Not an authoritative event log |
| Notification intent | Policy-controlled communication request | Per-recipient/channel delivery evidence |

AF never claims exactly-once execution or delivery. Durable handlers are idempotent and use
deduplication/inbox state where required. Queue acknowledgement follows required durable
effects. Externally visible jobs, events, mail, notifications, and broadcasts publish after
the authoritative transaction commits.

Ordering, replay, retention, consumer groups, acknowledgement, visibility, redelivery, and
dead-letter behavior are capabilities documented and tested per driver.

## Error taxonomy and kernels

Internally AF distinguishes configuration, resolution, capability, validation, authorization,
transport, transient, permanent, integrity, cancellation, and shutdown failures. Protected
traces retain typed cause and lifecycle stage; untrusted interfaces receive sanitized reason
codes.

HTTP Web, API, CLI, TUI, worker, scheduler, pub-sub, broadcast, and communication kernels use
the same `Application`, scoped context, application command/query boundary, policies,
provenance, and telemetry. Each retains transport-specific middleware, serialization,
authentication, exception mapping, and graceful drain. No transport queries a backend
directly, bypasses policy, or reimplements business semantics.
