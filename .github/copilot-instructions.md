# AgentFramework / AF

AgentFramework contains AF, an idiomatic Python 3.12 agent framework. AF is
classification-heavy and inference-light: supported work follows
`classify -> route -> execute -> verify -> record -> test -> reuse`. Repeated
work should become deterministic, verified, and reusable instead of requiring
fresh generative inference.

The historical agent definitions and governance documents remain part of the
repository. Preserve their validators and append-only records while extending
the Python application.

## Source map

- `src/af/framework/`: transport- and persistence-neutral contracts, container,
  routing, execution pipeline, reuse, types, and framework errors.
- `src/af/domain/`: AF classifiers, procedures, verification, and the persisted
  `Task` model/repository port.
- `src/af/application/`: use cases and the `TaskController` used by every
  transport.
- `src/af/infrastructure/`: SQLAlchemy repositories, database lifecycle, and
  trace persistence.
- `src/af/interfaces/`: Typer CLI, FastAPI HTML/REST composition, Strawberry
  GraphQL, presenters, and FastMCP.
- `src/af/views/templates/`: Jinja HTML views.
- `src/af/bootstrap.py`: composition root and dependency-injection providers.
- `src/af/config.py`: environment-backed settings.
- `tests/unit/`, `tests/integration/`, and `tests/feature/`: behavior at each
  boundary.

Dependencies point inward. Framework code must not import application,
infrastructure, interfaces, or bootstrap code. Domain code must remain free of
transport and ORM frameworks. Application code coordinates framework and
domain ports. Infrastructure implements ports. Interfaces translate external
input/output and delegate to `TaskController`; they do not reimplement business
rules or query SQLAlchemy directly. `bootstrap.py` is the one place allowed to
compose all layers.

## Required way of working

1. Treat the assigned issue and its file list as a hard scope boundary. Do not
   refactor unrelated files or overwrite another issue's work.
2. Work test-first: add or identify a failing behavioral test, make the smallest
   implementation pass, then remove duplication.
3. Inject dependencies through constructors or factories. Avoid global mutable
   state and hidden service locators.
4. Add precise type hints to public Python APIs and keep framework-specific
   objects at adapter boundaries.
5. Keep all interfaces behaviorally consistent. `hello` returns
   `Hello, world!`; `how many rs in strawberry` is classified and computed by
   the reusable character-count procedure and returns `3`. Never hard-code that
   one answer or use an LLM for it. Deterministic routes run before any selector,
   and a selector may only propose an installed typed procedure. The reference app
   ships no real selector adapter, and remote selector execution is denied by the
   current auto-execution policy.
6. Persisted `Task` CRUD must go through the shared application controller.
7. Never fabricate test output, citations, or completed work. Report commands
   exactly as run.
8. `specs.md`, `decisions.md`, and `agent_runs.md` are append-only. Never edit or
   remove existing entries.

## Bootstrap and checks

Use uv and the committed lockfile:

```bash
uv sync --locked --extra dev
```

The repeatable development runner is authoritative. Preview first when changing
the workflow itself; execute it before completing code work:

```bash
python scripts/af_dev.py --action check
python scripts/af_dev.py --action check --execute
```

The executed check runs Ruff formatting and lint checks, strict mypy, pytest,
and the legacy `validate_agents.sh` suite. Use narrower actions (`test`, `lint`,
`format-check`, `typecheck`, or `legacy`) during iteration. Do not bypass or
weaken a check to make a change pass.
