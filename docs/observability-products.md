# Optional inspector, metrics and admin products

These products are observational extensions around shared application services.
They do not verify tasks, grant execution authority or determine task completion.
Inspector and metrics stores can be enabled independently; disabling either
leaves ordinary task dispatch and persistence unchanged. The reference settings
are `AF_INSPECTOR_ENABLED` and `AF_METRICS_ENABLED`, both opt-in. No remote telemetry
service is required.

## Inspector

`InspectorService` accepts installed event names, installed tags, a sampling rate,
a retention window and a row limit. It supports request, job, query, event, cache
and log categories. A collector must explicitly select a category and event name;
this is not an automatic capture of every HTTP body, database statement or log
line. Registered sources can call `collect` or publish through
`InspectorTelemetrySink`.

Only bounded, approved metadata survives collection: method, status, elapsed
milliseconds, byte/attempt/count values and registered operation names. Unknown
fields, including nested payloads and authorization values, are removed before
the store receives an entry. Their names and values are not stored; a count of
redacted fields is retained. Event/correlation/tenant references are hashed. Hashes
are useful for correlation but are not a guarantee of anonymity for guessable
identifiers. Raw prompts, outputs, SQL text, credentials and arbitrary log messages
are not persisted by this product.

Sampling is deterministic from the event reference. A duplicate event ID cannot
replace an earlier record. Queries accept bounded category/name/tag filters and
at most 100 rows. Retention defaults to one day and 1,000 records; the configured
per-tenant cap cannot exceed 10,000, and the store also applies a global 10,000-row
cap. Expired rows are hidden on reads and removed on subsequent writes. The store
is deliberately finite; its history is not an authoritative audit ledger.

## Independent metrics

`MetricsService` accepts a finite registry of counters, gauges and histograms.
Labels must have installed finite value domains. There are at most 32 definitions,
256 combinations per definition and 1,024 declared combinations across the
registry. Unregistered names, arbitrary user/request IDs, unexpected labels,
nonfinite values and negative counter deltas are rejected.

Samples aggregate into minute buckets. Dashboard cards expose count, total,
minimum, maximum, latest value and histogram bucket counts. Histogram bounds are
explicit; the final bucket contains values beyond the last bound. Retention works
at minute granularity, so the boundary minute can include samples up to 59 seconds
older than the nominal window. Row limits can further shorten the retained
history. Inspector entries are never needed to calculate these metrics.

`MetricsTelemetrySink` accepts metric signals whose name matches a definition,
with numeric `value` and the installed string label attributes. Both telemetry
sinks reject explicitly foreign tenant contexts. Sink failures are best effort
and cannot convert an application failure into success.

## Shared dispatch and operator queries

`ObservationMiddleware` wraps the shared application bus. It records terminal
outcomes for execute, create, update and character-count commands after the inner
handler returns; create/update persistence therefore precedes observation.
Ordinary observer failures cannot replace the result, original exception or
cancellation. It records no request contents. Suggested definitions are:

```python
labels = (
    ("operation", ("execute", "create", "update", "count")),
    ("outcome", ("ok", "error", "cancelled")),
)
MetricDefinition("af.task.completed", MetricKind.COUNTER, labels)
MetricDefinition("af.task.duration", MetricKind.HISTOGRAM, labels, (1., 10., 100., 1000.))
```

Here `completed` means a terminal dispatch observation, with the outcome label;
it is not an assertion of independently verified task success.

Operator reads require `af:observability:read`. Purging requires
`af:observability:write` and explicit CLI confirmation. The tenant is fixed by
host composition; queries cannot choose another tenant.

```bash
af auth issue --subject operator --scope af:observability:read --output observe-token.json
af observe inspector --credential observe-token.json
af observe metrics --credential observe-token.json
```

For a separately issued write-scoped token:

```bash
af observe purge inspector --credential observe-write-token.json --confirm
```

Purge deletes only the selected product's tenant-scoped observational rows, not
application tasks, credentials or authoritative traces. Inspecting a disabled
product reports an unavailable error instead of silently installing/enabling it.

## Installed admin resources

`AdminService` composes a finite registry of `AdminResource` queries and
`AdminAction` commands. Each read/action declares its required scope. Actions
require explicit confirmation by default and are denied before dispatch if the
scope or confirmation is missing. Callbacks receive the authenticated principal
and bounded parameters; they call existing application services. The registry has
no direct ORM/repository access and provides no arbitrary command execution.

```bash
af observe admin-query RESOURCE --credential admin-token.json
af observe admin-action RESOURCE ACTION --credential admin-token.json --request parameters.json --confirm
```

The host chooses which identity, queue, schedule, flag, search or health resources
are installed. Missing resources/actions fail closed. A registered action remains
subject to the underlying application command's own policy, confirmation,
idempotency and verification rules. JSON rows/cards are transport-neutral; a host
can render them in an authorized web/TUI panel without duplicating business logic.

Tests cover redaction before SQLite persistence, sampling, finite retention,
restart, tenant isolation, registered metric domains, authorization, explicit
admin dispatch, failed observers, and cancellation preservation. They do not
assert a remote monitoring backend or exhaustive automatic instrumentation.
