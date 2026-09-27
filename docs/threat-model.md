# AF threat model

Version: `1.0.0`, 2026-09-26. Scope: the installed local-first alpha reference
application, its optional adapters, and its package/native tooling. This records
repository boundaries and review obligations; it is not an independent security
assessment, platform certification, or deployment authorization.

## Assets, actors and trust boundaries

Protect task content, account credentials and recovery material, signing/encryption
keys, consent and lease state, approved procedure/catalogue identities, local files,
artifacts, resource budgets, and the integrity of recorded evidence. Task CRUD
intentionally stores prompts and results; task storage is private application data,
not a sanitized telemetry store. Backups and exported credentials need the same
protection as their source. Expired credentials or a deleted task do not erase copies
already exported by an authorized caller.

Treat HTTP/MCP/CLI input, source documents, model proposals, peer packets, external
service responses and imported content as untrusted data. An installed provider,
Python application factory and SDK client are trusted host code, not a sandbox.
The local administrator controls composition and private state; compromise of that
OS identity or its keys is outside the in-process isolation guarantee. Default
in-process repositories and test doubles do not establish hostile multi-tenant isolation.

The important boundaries are input-to-typed-request; selector-to-installed-procedure;
policy/consent-to-execution; signed peer/content-to-local-use; application-to-driver;
durable state-to-observational signal; and source/lock-to-built artifact. A claim
crossing one boundary does not establish authority at the next.

## Threats and implemented controls

| Threat | Current control and evidence location | Limits and required review |
| --- | --- | --- |
| Hostile/ambiguous input and selector manipulation | Bounded typed inputs, installed descriptor validation, deterministic-first selection and independent result verification; `test_pipeline.py`, `test_model_selectors.py` | Review every contributed effect/schema/verifier; a model proposal is not authorization |
| Silent inference or data egress | Explicit configured providers, remote policy/consent and fail-closed unavailable routes; `test_decision_selectors.py`, decision-provider tests | No blanket no-egress claim for arbitrary installed plugins or an OS without a firewall |
| Browser forgery, injection and credential disclosure | Signed double-submit CSRF on HTML mutations, strict Origin/Referer handling, trusted Host, escaped Jinja, same-origin CSP, text-only local explorers; HTTP and web-accessibility tests | Headless bearer support does not supply browser login; authenticated ingress and TLS deployment remain host responsibilities |
| API abuse and exhaustion | Bounded bodies and prompts, GraphQL complexity controls, typed operation handling, conditional/idempotency contracts; HTTP/GraphQL/API tests | In-process deadlines cannot preempt all synchronous third-party SDK calls; proxy/server quotas require deployment evidence |
| Credential theft, brute force and replay | Hashed tokens, scoped/tenant-bound authentication, rotation/revocation, password attempt windows, single-use reset/MFA/passkey challenges; authentication/account tests | Local export does not prove mailbox ownership; physical authenticators and live identity providers need separate qualification |
| Malicious peer or replayed work | Pinned paired identities, signed exact leases/envelopes, replay/expiry/revocation checks, independent result acceptance and bounded IPC; trusted-peer, lease-integrity and peer-IPC tests | Pairing is not permission for arbitrary execution; DHT/relay and physical cross-platform evidence remain separately recorded |
| Malicious content, path traversal and supply substitution | Digest/signature/trust checks, explicit quarantine, bounded content acquisition, safe logical filesystem keys and symlink checks; trusted-content and filesystem tests | SFTP lacks a portable no-follow primitive against hostile concurrent root changes; remote roots must be controlled |
| Duplicate or lost asynchronous effects | Durable claims, owner-fenced receipts, transactional local streams/outbox, uncertain-delivery states and no blind SMTP retry; queue/stream/notification tests | No generic exactly-once delivery; SMTP acceptance is not delivery; external service/crash-gap reconciliation is explicit |
| Private observations becoming data exfiltration | Finite inspector metadata, no arbitrary log bodies, task/trace separation and explicit local output files; observability/privacy tests | Review each new sink and raw trace migration; hashes of guessable values are not anonymity |
| Locale or source text changing policy | Declarative locales, separate language/territory presentation, controlled Human Interpreter, source attribution and WHAT/SHOULD/HOW separation | Translation certification and critical-text human review remain independent of parser correctness |
| Supply-chain and evidence substitution | Exact lock, source-to-wheel/RECORD validation, runtime-only hashed offline install, SBOM/security tooling and candidate-bound signing helpers | Tool execution is not independent review; publication, signing-key governance, required checks and release approval remain separate |

Read [driver support](driver-support.md), [authentication](authentication.md),
[local engineering](local-engineering.md), [durable streams](durable-streams.md),
and [production MVP](production-mvp.md) for precise adapter and deployment constraints.
The finite engineering route executes only its installed, test-gated template family;
it does not authorize arbitrary user/model/peer source code.

## Review and release obligations

The first production candidate profile is the narrower dedicated-account, single-node
MCP-stdio profile. Network listeners, public identity hosting, DHT/native hosting and
other profiles require their own acceptance evidence. Loopback and repository privacy
do not authenticate a caller.

Remaining external/candidate-bound obligations include independent security and privacy
review; advisory/static/secret scan disposition; exact dependency and artifact provenance;
physical Windows/macOS/Linux and mobile/native interoperability; firewall/no-egress proof;
live database/cloud/identity/mail service qualification; restore/rollback exercises; and
the approval authorities required by the release manifest. A skipped opt-in test is unknown,
not passing evidence. No high/critical or actionable finding may be relabelled accepted
solely because a focused suite passes.

Report suspected vulnerabilities privately as described in [SECURITY.md](../SECURITY.md).
Record a bounded reproduction, affected boundary, actual impact, mitigation and regression;
avoid real credentials, recipients or production data in reproductions.
