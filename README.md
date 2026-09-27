# AF Agent Framework

AF is a Laravel-inspired, Python-native way to turn repeated agent work into tested,
deterministic procedures. Its execution kernel follows:

`classify → route → execute → verify → record → test → reuse`

The same application service powers a Typer CLI, server-rendered HTML MVC, REST,
GraphQL, XML/SOAP, and MCP. Deterministic static and procedural routes run first and bypass any
optional selector. A selector may only propose an installed procedure; AF does not ship a
model-generated-answer route.

The reference application includes:

- `hello` → `Hello, world!`
- `how many rs in strawberry` → `3`, computed by a reusable character loop and checked
  by an independent verifier
- a finite independently verified math/physics catalogue and locale-aware presentation
- durable typed workflows, restartable schedules, and scoped headless tokens/OAuth
- local initialization, a typed terminal application, and six application-owned starter kits
- finite algorithms/problems and a controlled Human Interpreter with source provenance
- headless accounts/MFA/passkeys, durable local streams, and optional inspector/metrics
- persisted Task create/list/read/update/delete operations
- SQLite in normal use and in-memory repositories in tests
- an MCP server for local clients plus deployment guidance for ChatGPT-compatible HTTPS or
  Secure MCP Tunnel access

The package is currently pre-release software at version `0.1.0a0` until roadmap issue #3
quality gates are complete.

The first candidate deployment target is the deliberately narrow
[private single-node production-MVP profile](docs/production-mvp.md). The current declaration
report is incomplete and names every release blocker; neither the report, repository privacy,
nor loopback defaults provide production certification.

## Quick start

AF requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync --locked --extra dev
uv run af hello
uv run af ask "how many rs in strawberry"
uv run af route-agent "Implement the endpoint"
```

Expected output:

```text
Hello, world!
3
{"prompt": "Implement the endpoint", "status": "routed", "agent": "builder", "reason": "specialist_match", "candidates": ["builder"]}
```

Task CRUD is available from the same CLI:

```bash
uv run af tasks create "how many rs in strawberry"
uv run af tasks list
uv run af tasks show TASK_ID
uv run af tasks update TASK_ID "how many bs in blueberry"
uv run af tasks delete TASK_ID
```

By default, persistent data is stored at `storage/af.sqlite3`. Override this with
`AF_DATABASE_URL` for an unmanaged application. A managed declaration created by
`af init` is loaded by the default CLI and MCP factories; its state directory is selected
by `AF_MACHINE_ROOT` (default `storage`). Explicit application factories own their configuration.

Create an application whose source you own:

```bash
uv run af new my-app --template full-stack --package my_app
```

The six templates are `basic-web`, `pretty-web`, `rest-api`, `worker`, `tui`, and
`full-stack`. Each includes its own composition root, primary-entrypoint tests and CI.
See [starter kits](docs/starter-kits.md) for installation and execution.


## Integrated local operations

```bash
uv run af init --plan --profile scheduler
# After reviewing the local plan:
uv run af init --non-interactive --apply --yes --profile scheduler
uv run af status
uv run af data init
uv run af doctor --json
uv run af ask "math divide a=1 b=3" --language fr --territory CA --json
uv run af workflows start '{"texts":["strawberry","raspberry"],"character":"r"}' --max-nodes 1
uv run af workflows resume RUN_ID
uv run af data backup BACKUP_PATH
uv run af capabilities --json
uv run af catalogues algorithms
uv run af human interpret 'sort integers [3,1,2]'
uv run af tui run tasks --plain
uv run af peer-demo --execute
```

Initialization is optional for the basic deterministic examples. The managed profiles
provision only local SQLite components; they do not install model runtimes or contact
remote services. `--plain` displays a terminal snapshot without reading input or running a task.

The peer demo uses temporary loopback nodes and the installed verified character
procedure. It is qualification evidence for that local path, not a promise about
remote connectivity or mobile hosting. `af self-improve --fixture` and `af evolve
--fixture` explicitly exercise offline evidence fixtures; configured self-operation
ports and runtime admission remain separate.

`AF_SELECTOR_PROVIDER=ollama`, `AF_SELECTOR_MODEL=MODEL`, and an optional
`AF_SELECTOR_ENDPOINT` enable a loopback selector (600-second startup/request bound).
Deterministic procedures still bypass it. Jev requires `AF_DECISION_ENABLED=true`,
`AF_ALLOW_REMOTE_INFERENCE=true`, `TYPESAFE_API_KEY`, and the explicit
`af decisions preview` → `approve` → `evaluate` flow. Neither provider is required.

See [durable workflows](docs/durable-workflows.md), [data lifecycle](docs/data-lifecycle.md),
[science](docs/science-catalogue.md), [locales](docs/locales.md),
[scheduling](docs/scheduling.md), [driver qualification](docs/driver-support.md), and
[agent development](docs/agent-development.md). The current finite implementation
and measured support limits are documented; roadmap completion is still under validation.

Additional installed journeys: [machine setup](docs/machine-setup.md),
[terminal applications](docs/tui.md), [algorithms/problems](docs/computational-catalogues.md),
[Human Interpreter](docs/human-interpreter.md), [human decisions](docs/human-decisions.md),
[accounts and authentication](docs/authentication.md), [durable streams](docs/durable-streams.md),
and [observability products](docs/observability-products.md).

## HTML, REST, and GraphQL

Start the HTTP application:

```bash
uv run af serve
```

Then open:

- HTML MVC: `http://127.0.0.1:8000/`
- Local REST documentation/explorer: `http://127.0.0.1:8000/docs`
- Local GraphQL explorer: `http://127.0.0.1:8000/graphql/explorer`
- Machine-readable OpenAPI: `http://127.0.0.1:8000/openapi.json`

Both explorers use locally served assets under the existing same-origin content-security
policy. They execute only on an explicit submission, preserve server authorization, and
display responses as text. `/graphql` itself remains the JSON API. See
[web access and API explorers](docs/web-accessibility.md) for keyboard behavior and current
accessibility evidence. Authenticated browser hosting requires an explicit host authentication
integration; headless bearer support is not a browser login UI.

REST example:

```bash
curl -sS http://127.0.0.1:8000/api/v1/tasks \
  -H 'content-type: application/json' \
  -d '{"prompt":"how many rs in strawberry"}'
```

GraphQL example:

```bash
curl -sS http://127.0.0.1:8000/graphql \
  -H 'content-type: application/json' \
  -d '{"query":"mutation { createTask(input: {prompt: \"how many rs in strawberry\"}) { id output verified } }"}'
```

REST and GraphQL share the same Task repository and application controller; transport
modules do not duplicate counting, verification, or persistence rules.

### Operations dashboard

The read-only operations projection is available through the same application boundary:

```bash
uv run af operations
uv run af operations --json
curl -sS http://127.0.0.1:8000/api/v1/operations
```

Open `http://127.0.0.1:8000/operations` for the server-rendered view. The projection is
bounded and excludes prompts, outputs, trace identifiers, credentials, and configuration.
Optional bearer enforcement is available with `AF_HTTP_AUTH_REQUIRED=true` and scoped
locally issued tokens. Loopback defaults and token support do not establish deployment qualification.
See [Operations dashboard](docs/operations-dashboard.md).

## MCP and ChatGPT

Run AF as a local stdio MCP server:

```bash
uv run af-mcp
```

For local Streamable HTTP development:

```bash
AF_MCP_TRANSPORT=streamable-http uv run af-mcp
```

This binds to `127.0.0.1:8000/mcp` by default. ChatGPT web cannot connect directly to a
local stdio or loopback server. For local/private use, prefer an outbound-only Secure MCP
Tunnel deployment. Public HTTPS is a separate authenticated and hardened option. AF includes explicitly configured loopback Ollama/OpenAI-compatible selector adapters
and a separate consented Jev decision adapter. Remote selector auto-execution remains
denied by policy; no provider result grants execution authority. The repository skill in
`.agents/skills/af-work/` teaches a compatible agent when to use each AF tool without
reimplementing AF logic.

See [ChatGPT and MCP](docs/chatgpt-mcp.md) and [Architecture](docs/architecture.md).
The approved future execution policy is documented in
[Capability Ladder and Acceptance Scopes](docs/capability-ladder.md). The active runtime
continues to deny direct generation until its scoped review and reuse gates are implemented.
The Laravel-inspired provider, lifecycle, manager, driver, scope, data, and delivery
contracts are defined in [Application Platform Contracts](docs/application-platform.md).
Testing hosts, deterministic fakes, provider suites, driver suites, and their non-certifying
boundary are defined in [Testing and Conformance](docs/testing-conformance.md).
Queue delivery, ordering, and unsupported-capability semantics are defined in
[Queue contracts](docs/queue.md).

## Prime Language / Ideon reference

The first deterministic Prime Language slice is available through the shared Ideon
application boundary. It preserves unresolved senses instead of guessing, emits all eleven
inquiry fields, and keeps a stable numeric Ideon key separate from the immutable graph
revision.

```bash
uv run af ideon interpret "sat on the bank"
uv run af ideon interpret "sat on the bank" --actor alice --domain river
```

The equivalent REST endpoint is `POST /api/v1/ideons/interpret`:

```bash
curl -sS http://127.0.0.1:8000/api/v1/ideons/interpret \
  -H 'content-type: application/json' \
  -d '{"surface":"sat on the bank","source_id":"example:1","rights":"user-provided"}'
```

This is a controlled reference dictionary and deterministic contract, not a general English
parser or a trained language model. See [Prime Language / Ideon implementation](docs/prime-language-ideon-implementation.md).

## Development workflow

The repeatable development gate is registered as `[SCRIPT:af-dev]` and previews commands
unless execution is explicit:

```bash
python3 scripts/af_dev.py --action check
python3 scripts/af_dev.py --action check --execute
```

The executed gate checks Ruff formatting and linting, strict mypy typing, pytest, and the
legacy AgentFactory governance suite. It also validates the production-MVP manifest and
builds, inspects, temporarily installs, and smokes a wheel produced from the sdist. The
default framework tests use local/in-process fixtures. Real-service tests are opt-in and skipped
without their configured services; a skip does not establish support. The complete gate also
includes local protocol tests and a runtime-only temporary package installation. That installation
uses hashed locked dependencies from a primed offline cache and does not borrow development
site packages; it is evidence on the current host, not a clean OS or cross-platform certification.

Long-running epic analysis uses the registered `[SCRIPT:af-epic-worker]` harness. Each
iteration records a checkpoint under a stable session ID so service restarts preserve one
continuity stream. The unattended worker is checkpoint-only: it runs a fixed primary agent in
private OpenCode configuration and data roots, verifies the resolved configuration and runtime
permissions before dispatch, denies shell/network/subagent authority, and cannot publish or claim
acceptance. A configured local model is reduced to one strictly validated loopback
OpenAI-compatible provider entry; unrelated global OpenCode policy is not inherited. Reviewed
publication and closure remain separate operator actions.

```bash
AF_EPIC_NUMBER=177 \
AF_EPIC_MODEL=lovelace-general/qwen3.6-27b \
AF_EPIC_MEMLOG_SESSION_ID=agentframework-epic-177-worker \
scripts/af_epic_worker
```

All repository, state, lane, model, timeout, polling, OpenCode, and memlog settings are
environment-configurable. Deploy the tracked script rather than maintaining an unversioned copy.

Inspect canonical policy declarations separately; a complete declaration report still does
not verify evidence or authorize production deployment:

```bash
python3 scripts/af_dev.py --action release-report --execute
python3 scripts/af_dev.py --action release-check --execute
```

## Repository governance

AF retains the original AgentFactory registry and documentation conventions:

- [AGENTS.md](AGENTS.md) — contributor and coding instructions
- [agents.yaml](agents.yaml) and [agents.md](agents.md) — agent definitions
- [specs.md](specs.md), [decisions.md](decisions.md), and
  [agent_runs.md](agent_runs.md) — append-only history
- [.github/copilot-instructions.md](.github/copilot-instructions.md) — GitHub Copilot
  repository instructions

Do not rewrite append-only history. Add a dated entry at the end and run
`./validate_agents.sh`.

The [Constitution](docs/constitution.md), [trace privacy](docs/trace-privacy.md),
[configured self-development](docs/self-development.md),
[threat model](docs/threat-model.md) and [accessibility evidence](docs/web-accessibility.md)
record the implemented boundaries and the remaining independent/manual checks. Passing repository
tests does not close the roadmap, approve a release, certify external drivers, or authorize deployment.

## License

Copyright © 2019 Alexander Holman. All rights reserved; see [LICENSE](LICENSE).
