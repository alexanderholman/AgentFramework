# Contributing

AgentFramework is a Python 3.12 project. Contributions must follow the repository
instructions in [AGENTS.md](/AGENTS.md), the code of conduct, and the append-only
governance rules.

## Workflow

1. Search or open an issue in
   [`alexanderholman/AgentFramework`](https://github.com/alexanderholman/AgentFramework/issues).
2. Create one focused branch per issue using `agent/<description>`.
3. Add or identify a failing behavioural test before changing implementation.
4. Keep framework, domain, application, infrastructure, and interface dependencies
   aligned with [the architecture](/docs/architecture.md).
5. Run the complete quality gate.
6. Open a pull request, describe the impact and verification, and obtain review before
   merge.

```bash
uv sync --locked --extra dev
python3 scripts/af_dev.py --action check --execute
```

The gate checks Ruff formatting and linting, strict mypy, pytest with at least 90 percent
coverage, and the inherited agent/governance validations.

## Boundaries

- Put shared behaviour in the application or domain layers, not separately in each CLI,
  HTML, REST, GraphQL, or MCP adapter.
- Prefer static and procedural routes; they run before any optional selector and remain
  independently verified. A selector may only propose an installed procedure, AF ships no
  direct model-generated-answer route, and remote selector execution is currently denied.
- Never rewrite `specs.md`, `decisions.md`, or `agent_runs.md`; append a superseding,
  dated entry.
- Never commit credentials, generated caches, virtual environments, coverage artefacts,
  or runtime databases.
- Report vulnerabilities privately as described in [SECURITY.md](/SECURITY.md).

The repository is all-rights-reserved unless the owner grants permission in writing;
opening a pull request does not change the licence.
