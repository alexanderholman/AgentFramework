#!/usr/bin/env python3
"""Fail closed unless OpenCode resolved the epic checkpoint policy exactly."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, cast

CHECKPOINT_AGENT = "af-epic-checkpoint"
CHECKPOINT_SYSTEM_PROMPT = (
    "Operate only as an untrusted local checkpoint analyst. Never claim acceptance or "
    "completion, and never attempt remote mutation or permission bypass."
)
EXPECTED_PERMISSIONS: dict[str, Any] = {
    "*": "deny",
    "read": {
        "*": "allow",
        "*.env": "deny",
        "*.env.*": "deny",
        "*.env.example": "allow",
    },
    "glob": "allow",
    "grep": "allow",
    "edit": {
        "*": "allow",
        ".git": "deny",
        ".git/**": "deny",
        ".opencode": "deny",
        ".opencode/**": "deny",
    },
    "lsp": "allow",
    "todowrite": "allow",
    "bash": "deny",
    "task": "deny",
    "skill": "deny",
    "question": "deny",
    "webfetch": "deny",
    "websearch": "deny",
    "external_directory": "deny",
    "doom_loop": "deny",
}


def _fail(detail: str) -> None:
    raise RuntimeError(f"effective OpenCode checkpoint policy is unsafe: {detail}")


def _runtime_fail(detail: str) -> None:
    raise RuntimeError(f"runtime checkpoint policy is unsafe: {detail}")


def render_config() -> dict[str, Any]:
    return {
        "default_agent": CHECKPOINT_AGENT,
        "permission": copy.deepcopy(EXPECTED_PERMISSIONS),
        "agent": {
            CHECKPOINT_AGENT: {
                "description": "Checkpoint-only epic backlog analyst",
                "mode": "primary",
                "disable": False,
                "prompt": CHECKPOINT_SYSTEM_PROMPT,
                "permission": copy.deepcopy(EXPECTED_PERMISSIONS),
            }
        },
        "mcp": {},
        "plugin": [],
    }


def verify(config: dict[str, Any]) -> None:
    if config.get("default_agent") != CHECKPOINT_AGENT:
        _fail("checkpoint agent is not the default")
    if config.get("permission") != EXPECTED_PERMISSIONS:
        _fail("global permissions differ from the checkpoint policy")

    selected = config.get("agent", {}).get(CHECKPOINT_AGENT)
    if not isinstance(selected, dict):
        _fail("checkpoint agent is missing")
    if selected.get("mode") != "primary" or selected.get("disable") is not False:
        _fail("checkpoint agent is not an enabled primary agent")
    if selected.get("prompt") != CHECKPOINT_SYSTEM_PROMPT:
        _fail("checkpoint system prompt differs")
    if selected.get("permission") != EXPECTED_PERMISSIONS or "tools" in selected:
        _fail("checkpoint agent permissions differ")

    if config.get("mcp"):
        _fail("MCP servers remain configured")
    if config.get("plugin") or config.get("plugin_origins"):
        _fail("plugins remain configured")


def verify_agent(agent: dict[str, Any], policy_root: Path) -> None:
    if agent.get("name") != CHECKPOINT_AGENT or agent.get("mode") != "primary":
        _runtime_fail("the selected agent is not the checkpoint primary")
    if agent.get("prompt") != CHECKPOINT_SYSTEM_PROMPT:
        _runtime_fail("the checkpoint system prompt differs")

    tools = agent.get("tools")
    if not isinstance(tools, dict):
        _runtime_fail("resolved tools are missing")
    tools = cast(dict[str, Any], tools)
    for name in ("bash", "task", "webfetch", "websearch", "skill", "question"):
        if tools.get(name) is not False:
            _runtime_fail(f"dangerous tool remains enabled: {name}")

    raw_rules = agent.get("permission")
    if not isinstance(raw_rules, list) or not all(isinstance(rule, dict) for rule in raw_rules):
        _runtime_fail("resolved permission rules are malformed")
    rules = cast(list[dict[str, Any]], raw_rules)
    for permission in ("bash", "task", "webfetch", "websearch", "skill", "question"):
        action = None
        for rule in rules:
            if rule.get("pattern") == "*" and rule.get("permission") in ("*", permission):
                action = rule.get("action")
        if action != "deny":
            _runtime_fail(f"dangerous permission is not denied: {permission}")

    last_external_deny = max(
        (
            index
            for index, rule in enumerate(rules)
            if rule.get("permission") == "external_directory"
            and rule.get("pattern") == "*"
            and rule.get("action") == "deny"
        ),
        default=-1,
    )
    expected_tool_output = str(policy_root / "xdg-data/opencode/tool-output/*")
    later_external_allows = [
        rule.get("pattern")
        for rule in rules[last_external_deny + 1 :]
        if rule.get("permission") == "external_directory" and rule.get("action") == "allow"
    ]
    if later_external_allows != [expected_tool_output]:
        _runtime_fail("external-directory access is not limited to private tool output")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--config", type=Path)
    action.add_argument("--agent-config", type=Path)
    action.add_argument("--render-config", action="store_true")
    parser.add_argument("--policy-root", type=Path)
    args = parser.parse_args()
    if args.render_config:
        print(json.dumps(render_config(), separators=(",", ":")))
        return 0
    try:
        source = args.agent_config or args.config
        assert source is not None
        config = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            _fail("debug output is not an object")
        if args.agent_config is not None:
            if args.policy_root is None:
                _runtime_fail("--policy-root is required with --agent-config")
            verify_agent(config, cast(Path, args.policy_root))
        else:
            verify(config)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"epic-worker-policy-check: failed: {error}")
        return 1
    print("epic-worker-policy-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
