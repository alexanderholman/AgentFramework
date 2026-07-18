# WIP

## Active
- Issue #1: AF Python vertical slice and ChatGPT MCP adapter.
  - Script: `[SCRIPT:af-dev]`
  - Branch: `agent/af-python-vertical-slice`
  - Gate: `python3 scripts/af_dev.py --action check --execute`
  - Status: Local implementation and validation complete; publication to the private
    `alexanderholman/AgentFramework` repository is in progress through the authenticated
    GitHub app.

## Next
- Publish the branch, open a draft PR, and request
  `copilot-pull-request-reviewer[bot]`.
- After the setup workflow reaches `master`, assign bounded follow-up issues to
  `copilot-swe-agent[bot]` so each starts from the tested AF baseline.

## Done
- AF Python package, kernel, deterministic examples, Task CRUD, all five transports, MCP,
  repository skill, Copilot onboarding, documentation, and 90-percent coverage gate.
