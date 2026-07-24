# Observability contracts

AF observability is optional evidence, never authority. Exporter failure must not change application results, authorization, cleanup, or routing.

## Context propagation

`TelemetryContext` is the bounded process-envelope form of observability context. It carries correlation, causation, trace/span, operation, and a caller-supplied tenant-safe class. It does not carry a principal, claims, roles, scopes, credentials, or a raw tenant identity. Reconstructing a full `ExecutionContext` therefore requires an already-authorized local template; the carrier cannot grant or widen authority.

The carrier is versioned and fail-closed. Unknown `af.context.*` fields, unsupported versions, malformed flags, and invalid identifiers are rejected rather than ignored.

## Health

Health is not one boolean. `HealthObservation` keeps liveness, readiness, degradation, capability compatibility, reason, observation time, and freshness separate. A process may be live while warming, degraded, capability-mismatched, draining, saturated, unavailable, or quarantined. Consumers must reject stale observations and must not reinterpret one dimension as another.

## Redaction and cardinality

All structured records pass through `TelemetryPolicy` before optional exporters. Raw prompts, bodies, payloads, content, vectors, embeddings, and paths are rejected. Credential-like values are replaced with a constant redaction marker. Request-, subject-, tenant-, task-, session-, trace-, span-, correlation-, and causation-specific values are not allowed as metric labels or ordinary attributes.

Only bounded scalar values are accepted. Metric labels are stricter than log/span attributes and are intended for finite, stable categories rather than identities.

## Exporters

`SafeTelemetry` isolates ordinary exporter exceptions and exposes count-only diagnostics. A failing optional exporter cannot corrupt the underlying application operation. Cancellation, interpreter termination, and other `BaseException` control flow are not swallowed.

The framework package does not require an OpenTelemetry SDK. An optional bridge may implement the exporter protocol later, after applying AF policy and without changing these contracts.
