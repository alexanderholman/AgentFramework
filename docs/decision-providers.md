# Typed decisions and optional providers

The deterministic AF path needs no model, remote account or provider SDK. Core
`DecisionRequest` and `DecisionAnswer` values contain provider-neutral binary and
choice contracts. Probabilities and provider confidence are distinct fields:
choice reduction uses the selected option's probability, while Jev's confidence
summarizes distribution concentration. Neither is proof of correctness or execution
authority. Ties, incomplete distributions, digest mismatches and insufficient
probability remain unresolved.

`DeterministicDecisionProvider` accepts an injected rule. `DecisionProcedureSelector`
can use any decision provider to choose among trusted, argument-bound proposals
for installed procedures. Procedure selection still passes catalogue identity,
version/digest, grounding, policy and verification gates. It cannot invent a
procedure, permission or executable body. Cancellation, timeout, malformed and
unavailable results are explicit and never trigger a silent provider fallback.

## Local model selection

Enable only an explicitly installed model/server:

```bash
export AF_SELECTOR_PROVIDER=ollama
export AF_SELECTOR_MODEL=qwen3:8b
export AF_SELECTOR_ENDPOINT=http://127.0.0.1:11434
```

`openai` selects the OpenAI-compatible local `/v1/chat/completions` contract;
`ollama` selects `/api/chat`. The adapters accept only loopback endpoints, bounded
strict structured JSON and exact installed procedure/version/digest pairs. They
make no model downloads, process starts or tool calls. `LocalModelSelector` also
accepts an injected `api_key` for an authenticated local runtime; it sends the key
only in the authorization header. The operator must configure the separately
administered server for local execution: a loopback endpoint does not prove that
that server never proxies upstream. Physical model execution needs its own
recorded qualification evidence.

## Optional Jev decision calls

Jev is an optional adapter for the official TypeSafe System One HTTP API. Enabling
it does not select it as the core's default execution authority. Set
`AF_DECISION_ENABLED=true`, `AF_ALLOW_REMOTE_INFERENCE=true`, and the environment
credential `TYPESAFE_API_KEY` to make explicit remote evaluation available. The
pinned default model is `jev-1.13.0`; the host configures its tariff using
`AF_DECISION_TOKEN_PRICE_USD`. A provider key alone is insufficient to send data.

Create a bounded local request file, for example:

```json
{"kind":"binary","question":"Does the text describe a fruit?","state":"A strawberry is red.","options":[]}
```

Then use the durable consent path:

```bash
af decisions schema
af decisions preview --request request.json > preview.json
af decisions approve --request request.json --preview preview.json --max-cost-usd 0.01 --output consent.json
af decisions evaluate --request request.json --consent consent.json
af decisions receipt --consent consent.json
af decisions revoke --consent consent.json
```

Preview is pure and discloses the destination, pinned model, request and payload
digests, byte count, expiry and conservative configured-tariff ceiling. Approval
binds one call to that exact disclosure and stores only a consent-token hash.
Evaluation atomically consumes consent before sending; restart, revocation,
expiry, changed request/model/destination, replay and simultaneous reuse cannot
create a second authorized call. Timeout and malformed responses consume the
attempt. No automatic retry or alternate provider is included. Receipts persist
status, usage and digests without raw prompts, API keys or consent secrets.

The cost quote is an application budget using the configured tariff and bounded
input estimate, not a provider billing guarantee. AF checks returned usage against
the quote. It does not claim control over an external provider's billing system.
Provider responses are bounded, duplicate JSON keys and nonfinite values are
rejected, and errors are sanitized. KeyboardInterrupt/SystemExit are preserved.

Protocol tests use injected HTTP transports and local databases. They establish
request/response, failure, replay and persistence behavior; they are not evidence
of a billed Jev request, installed local model or live-service qualification.

Primary contracts: [TypeSafe API](https://docs.typesafe.ai/api),
[confidence](https://docs.typesafe.ai/confidence),
[models](https://docs.typesafe.ai/models),
[Ollama chat](https://docs.ollama.com/api/chat),
[structured outputs](https://docs.ollama.com/capabilities/structured-outputs),
[llama.cpp server](https://github.com/ggml-org/llama.cpp/tree/master/tools/server).
