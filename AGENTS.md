# AgentFramework engineering contract

AF is a Python 3.12 framework for turning repeated work into typed, independently
verified procedures: `classify → route → execute → verify → record → test → reuse`.
This is the canonical contract for all coding agents, including OpenCode, Qwen,
Copilot, and ChatGPT. Load narrower `AGENTS.md` files only for the area being edited.

## Start here

```bash
uv sync --locked --extra dev
uv run af dev describe --json
uv run af procedures --json
uv run af schemas
uv run pytest tests/unit/PATH.py
python3 scripts/af_dev.py --action check --execute
```

The canonical full gate includes format/lint, strict mypy, pytest with >=90% branch
coverage, OpenCode route tests, build/inspection/clean-install smoke, and governance.
It requires a clean committed tree for the package step. Use focused checks while
editing; do not weaken the gate or describe a subset as the complete gate.
Run `./validate_agents.sh` before each checkpoint commit.

Mission continuation is recorded in `tasks/autonomous-completion.md`. Inspect current
Git status/branch before editing; never overwrite another worker's changes. Use one
mission branch and commit useful, tested checkpoints. User-granted engineering scope
supersedes historical single-issue restrictions. External destructive effects still
require actual authority. Runtime authorization remains part of the product.

## Source map

| Area | Location |
| --- | --- |
| Neutral execution, types, policy and ports | `src/af/framework/` |
| Procedures, science, Ideon meaning and verification | `src/af/domain/` |
| Shared use cases/controllers | `src/af/application/` |
| Persistence, service clients and process adapters | `src/af/infrastructure/` |
| CLI, HTTP, HTML, REST/XML, SOAP, GraphQL, MCP | `src/af/interfaces/` |
| Cache/queue/storage managers and driver contracts | `src/af/{cache,queue,filesystem,graph}/` |
| Composition/configuration | `src/af/bootstrap*.py`, `src/af/config.py` |
| Unit / service / cross-interface tests | `tests/{unit,integration,feature}/` |
| Migration engine and reference schema | `framework/migrations.py`, `infrastructure/reference_schema.py` |
| Reusable development and validation commands | `scripts/af_dev.py`, `scripts/registry.yaml` |

## Invariants

- Dependencies point inward. Framework/domain/application have no ORM, transport or
  concrete provider imports. Interfaces translate; shared use cases decide. Bootstrap
  composes dependencies explicitly. No hidden global mutable application state.
- Prefer exact reuse and deterministic execution. Selectors propose only installed,
  version/digest-bound procedures. Statistical output is never its own verifier or
  an authorization grant. No implicit remote fallback or arbitrary source execution.
- Persist durable truth before publishing observational events. Queue delivery, process
  exit, cache residency and provider acceptance are not verified task completion.
- Validate types, size bounds, ambiguity, effects, deadlines, ownership and cancellation
  at shared boundaries. Preserve corrupt/stale/replayed-state failures.
- Add a failing behavioral regression before fixing a defect. Cover relevant failures,
  races, restart and denial. Protocol doubles prove protocol behavior; live-service
  support claims require separately recorded real-service evidence.
- Keep modules cohesive, APIs typed, instructions concise, schemas machine-readable and
  test commands deterministic. Do not require a large-model context window to work.
- Read the relevant existing script/workflow before repeating a multistep operation.
  Register reusable scripts with parameters rather than multiplying one-off workflows.
- Never fabricate tests, citations, benchmarks, authority, approval or completion.
  Record material assumptions explicitly as `[ASSUMPTION]`.

## Add behavior

Use `af make:procedure --help` and the procedure contributor contracts for procedures;
use `ServiceProvider` for composition and manager driver registrations for adapters.
See `docs/application-platform.md`, `docs/testing-conformance.md`, `docs/queue.md`,
`docs/science-catalogue.md`, and `docs/driver-support.md` as needed. Keep one shared
behavior across applicable transports, with integration tests for public paths.

## Historical definition registry

`agents/`, `agents.yaml`, `specialisms/`, `traits/`, `skills/` and `workflows/` remain
validated repository assets. Every agent definition has these H2 headings in order:
Purpose, Inputs, Outputs, Behavior, Constraints. Register unique IDs, valid tags and
real paths in `agents.yaml`; follow `agents.md` for its template.

`specs.md`, `decisions.md`, and `agent_runs.md` are strictly append-only. Preserve
existing bytes and append dated entries for material specifications, decisions and
execution checkpoints. Do not rewrite history to make validation pass.
