# WIP

## Active
- Issue #1: AF Python vertical slice and ChatGPT MCP adapter.
  - Script: `[SCRIPT:af-dev]`
  - Branch: `agent/af-python-vertical-slice`
  - Gate: `python3 scripts/af_dev.py --action check --execute`
  - Pull request: https://github.com/alexanderholman/AgentFramework/pull/2
  - Status: Published as a draft; Copilot code review requested.

## Next
- Review and address actionable Copilot feedback, confirm GitHub Actions, then mark the
  pull request ready when the vertical slice is accepted.
- After the setup workflow reaches `master`, assign bounded follow-up issues to
  `copilot-swe-agent[bot]` so each starts from the tested AF baseline.

## Done
- AF Python package, kernel, deterministic examples, Task CRUD, all five transports, MCP,
  repository skill, Copilot onboarding, documentation, and 90-percent coverage gate.
