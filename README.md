# AF Agent Framework

AF is a Laravel-inspired, Python-native way to turn repeated agent work into tested,
deterministic procedures. Its execution kernel follows:

`classify → route → execute → verify → record → test → reuse`

The same application service powers a Typer CLI, server-rendered HTML MVC, REST,
GraphQL, and MCP. Deterministic static and procedural routes run first and bypass any
optional selector. A selector may only propose an installed procedure; AF does not ship a
model-generated-answer route.

The first vertical slice includes:

- `hello` → `Hello, world!`
- `how many rs in strawberry` → `3`, computed by a reusable character loop and checked
  by an independent verifier
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
`AF_DATABASE_URL`.

## HTML, REST, and GraphQL

Start the HTTP application:

```bash
uv run af serve
```

Then open:

- HTML MVC: `http://127.0.0.1:8000/`
- OpenAPI: `http://127.0.0.1:8000/docs`
- GraphQL: `http://127.0.0.1:8000/graphql`

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
The HTTP surface remains loopback/development-only until AF has a real authentication boundary.
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
Tunnel deployment. Public HTTPS is a separate authenticated and hardened option. AF does
not bundle a real local or remote selector provider adapter, and remote selector execution
is denied by the current auto-execution policy. The repository skill in
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
framework tests are deliberately network-free.

Long-running epic execution uses the registered `[SCRIPT:af-epic-worker]` harness. Each
iteration records a checkpoint under a stable session ID so service restarts preserve one
continuity stream:

```bash
AF_EPIC_NUMBER=177 \
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

## License

Copyright © 2019 Alexander Holman. All rights reserved; see [LICENSE](LICENSE).
