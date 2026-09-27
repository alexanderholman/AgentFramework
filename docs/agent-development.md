# Agent development

`AGENTS.md` is the compact, model-independent engineering contract. Local instructions
in `src/af/AGENTS.md`, `src/af/interfaces/AGENTS.md`, and `tests/AGENTS.md` add detail
only for work in those directories. Copilot points to the same source; OpenCode
loads the root AGENTS file automatically. The project OpenCode configuration disables
sharing and does not select a provider/model or change an operator's global settings.

Start a bounded task by reading its source and neighboring tests. Use `af dev describe
--json`, `af procedures --json`, `af providers --json`, `af drivers --json`,
`af capabilities --json`, `af schemas NAME`, and `af doctor --json` for structured
contracts. `af capabilities` advertises only evidence from its supplied catalogue;
imported manifests do not authorize execution or prove readiness.

For a fresh OpenCode or Qwen worker, use the install/focused/full commands in AGENTS,
a small explicit scope, and the latest durable mission state. Do not load every
historical log into a prompt. Append important decisions and checkpoint results, with
remaining limitations, so a restarted worker can continue from the branch itself.
OpenCode JSON CLI output consists of events; consumers must not treat the entire
stream as a single task-result document.

Current upstream references checked 2026-09-26:
[OpenCode rules](https://opencode.ai/docs/rules/) and
[configuration](https://opencode.ai/docs/config/).
