# Machine API kernels

The installed HTTP application exposes versioned Task CRUD at `/api/v1/tasks`,
SOAP 1.2 at `/soap`, and GraphQL JSON at `/graphql`. All operations call the same
application controllers and authorization/context pipeline. The local interactive
API documents at `/docs` and `/graphql/explorer` use self-hosted assets.

## Conditional Task writes

Canonical JSON and XML Task create/read/update responses include a strong `ETag`.
Send that exact validator in `If-Match` on PATCH, PUT or DELETE. SQLite and the
in-memory reference repository compare the complete observed Task atomically at
write time; a competing mutation returns 412 without overwriting it. An already
stale validator is rejected before task execution. If another request wins during
execution, execution may have run, but its Task write fails. Conditional writes do
not roll back independently executed external effects.

This finite profile accepts one strong validator. Weak validators, lists and `*`
return 400. Omitting `If-Match` retains the existing unconditional API. JSON and XML
have distinct validators; optional localized presentations omit validators, so fetch
the canonical resource when performing a conditional write. Custom repositories
must implement `ConditionalTaskRepository`; unsupported conditional writes fail
rather than pretending to provide atomicity.

`Accept: application/json` and `Accept: application/xml` select equivalent resource
and application-error representations on the canonical Task routes. Quality values
and application wildcards are supported; unsupported formats return 406. JSON or
bounded UTF-8 XML `<task><prompt>…</prompt></task>` can be submitted independently of
the response format. DTDs, entities, processing instructions and unexpected XML
fields are rejected. The legacy `/api/v1/xml/tasks` routes remain available.

## Request replay and admission

Task CRUD mutations optionally accept `Idempotency-Key` (1–128 ASCII letters,
digits, `.`, `_`, `:`, `-`). The ledger binds its hash to tenant, subject and principal
type; an exact request fingerprint includes method, path, query, body, relevant
headers, current authorization claims and the configured API policy version.
Authentication executes again before replay. Changing request or policy with a
retained key returns 409. Responses are committed before delivery; replay returns
the original status, bytes and safe resource headers plus
`Idempotency-Replayed: true`. Cookies and credentials are never replay headers.
Idempotency does not apply to SOAP, GraphQL or other application APIs: such keys are
rejected so replay cannot bypass their independent authorization/security hooks.

The default file-backed SQLite Task composition creates a private sibling
`<database>.http.sqlite3` ledger. Replay survives HTTP/application restart. In-memory
Task repositories get an in-memory ledger; other database compositions must inject
a durable `HttpOperations` implementation explicitly. A caller-injected ledger is
caller-owned and must be closed by its owner. This adapter is SQLite-qualified;
there is no cross-database exactly-once transaction claim.

Completed non-server-error responses are retained for 24 hours, after which a key
may be used again. In-flight, crashed, server-error and oversized-response requests
remain **uncertain** indefinitely. They return 409 and are never automatically
re-executed. Operators must reconcile the actual side effect before choosing a new
key. The ledger holds at most 10,000 records and responses at most 1 MiB; capacity
exhaustion returns 503. Retained uncertain rows can exhaust capacity deliberately;
there is no unsafe automatic eviction or hidden retry.

A shared SQLite fixed-window limiter admits 600 requests per principal/tenant per
60 seconds, with at most 10,000 active identities. Anonymous loopback traffic shares
one bucket. Exhaustion returns 429 plus `Retry-After`; identity capacity returns
503. Inject `SQLiteHttpOperations` to configure bounded limits and retention. This
is application admission, not a replacement for network connection/body limits.
The policy-version parameter must be changed when authorization policy semantics
change independently of principal claims. Store the ledger in an operator-owned
private directory; replay bodies contain the same private data as Task responses.

## SOAP contract and security extension

`GET /soap?wsdl` and `/soap/wsdl` return deterministic WSDL 1.1 with SOAP 1.2
bindings. `/soap/schema.xsd` describes the installed `urn:af:v1` document/literal
operations and response fields. Contract addresses are relative, not derived from
untrusted Host headers. The closed registry exposes Hello, Ask and Task CRUD only.
Faults use the SOAP 1.2 envelope namespace, Sender/Receiver codes and sanitized
reasons; internal exceptions are never reflected.

Headers are rejected unless a `SoapSecurityVerifier` is explicitly injected into
`create_http_app(soap_security=...)`. With that verifier installed, every SOAP
operation must have exactly one WS-Security header. The verifier receives the
unchanged bounded envelope bytes, operation name and authenticated execution
context before dispatch. It must validate cryptographic signature/body coverage,
freshness and nonce replay, raising `SoapSecurityDenied` on failure. It cannot
replace or broaden the HTTP principal. AF supplies this hook, **not an implementation
of XML Signature, XML Encryption, UsernameToken or third-party WS-Security
interoperability certification**. Authentication and SOAP security hooks are
independent and both apply when configured.

Focused executable evidence is in `test_http_operations`, `test_http_ledger`,
`test_task_preconditions`, `test_http_middleware_boundaries`, `test_soap_contract`
and the existing cross-interface HTTP/XML tests. SQLite races, real application
restart, malformed XML, stale writes, policy/identity binding and uncertain replay
are covered without claiming an external service deployment.
