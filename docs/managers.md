# Managers, named connections, and driver capabilities

AF uses one provider-neutral pattern for replaceable infrastructure:

```text
Manager -> named ConnectionConfig -> registered DriverFactory -> ManagedDriver
```

The contract implements DEC-029 without pretending that different backends have identical
semantics. A manager owns configuration and lifecycle. A driver owns one live backend
connection or client. Application services ask for capabilities, not vendor identities.

## Resolution contract

- Configuration is copied into an immutable top-level snapshot when the manager is created.
- A default connection is optional. Calling `connection()` without one fails closed.
- Named connections are constructed lazily and cached by canonical connection name.
- Unknown connection names, unknown driver factories, malformed configuration, failed health
  checks, and missing capabilities fail without selecting another connection.
- Synchronous callers never start an event loop. Async factories require `aconnection()`.
- Concurrent resolution of one name shares one construction result.

Providers extend a manager explicitly during application boot:

```python
manager.extend("redis", redis_factory)
manager.extend_async("remote-search", remote_search_factory)
```

Factory replacement is rejected while an affected connection is cached or being constructed.
This prevents a live name from changing meaning underneath active work.

## Capabilities and health

Every managed driver supplies synchronous `HealthStatus` evidence. The evidence contains:

- a serving state (`healthy` or `degraded`) or a non-serving state;
- an immutable `CapabilitySet`;
- a canonical reason code when relevant;
- a monotonic revision.

Configured and call-specific requirements are checked before a driver is returned. Capabilities
that have already been admitted for a cached connection become part of its safety contract. If
a later health revision removes one, the manager retires and quarantines that connection. A
revision that moves backwards, or changes evidence without increasing, also quarantines it.
Restoration is explicit and creates a fresh driver; the manager never silently substitutes a
local, weaker, or differently configured backend.

The base contract records bounded runtime evidence. Backend packages remain responsible for
binding capability claims to their trusted manifests, probes, conformance evidence, policy,
and deployment profile where DEC-029 requires stronger proof.

## Explicit composition

Fallback, failover, and load balancing are represented by `CompositeDriver`, never hidden in
ordinary manager resolution:

- `FALLBACK` tries an ordered list declared by the caller;
- `FAILOVER` tries an ordered list declared by the caller;
- `ROUND_ROBIN` rotates across explicitly named eligible members.

Every member is still checked against the composite and per-member capability requirements.
If no member is eligible, the composite fails closed and retains the first protected failure as
diagnostic evidence. Initial `FALLBACK` and `FAILOVER` selection are intentionally equivalent;
the distinct mode preserves operator intent for policy, telemetry, and later specialized
recovery rules.

## Ownership

Managers dispose every constructed driver exactly once in reverse construction order, including
retired and quarantined instances. Async-only drivers require `aclose()`. Construction, factory
replacement, invalidation, and shutdown do not race through an active async construction flight.
