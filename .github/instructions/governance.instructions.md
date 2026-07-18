---
applyTo: "specs.md,decisions.md,agent_runs.md,agents/**/*.md,agents/*.md,agents.yaml,agents.md,project_context*.yaml,repo.yaml,scripts/**/*,workflows/**/*,tasks/**/*,traits/**/*,skills/**/*"
---

# AgentFramework governance requirements

- `specs.md`, `decisions.md`, and `agent_runs.md` are append-only. Never modify,
  reorder, or delete existing entries; append dated superseding entries instead.
- Agent definition files retain these H2 headings in this order: `Purpose`,
  `Inputs`, `Outputs`, `Behavior`, and `Constraints`. Registry IDs remain unique
  and tags must be allowed by `agents.yaml`.
- Mark uncertain claims with `[ASSUMPTION]`; do not fabricate citations, run
  evidence, or results.
- Follow the script-first Only Write Once policy. Check `scripts/registry.yaml`
  before inventing an ad-hoc repeatable command. New mutating scripts must be
  parameterized, replayable, and dry-run by default.
- Run `./validate_agents.sh` after governance, script, workflow, task, trait, or
  agent-definition changes.
