#!/usr/bin/env python3
"""Fail closed unless OpenCode resolved the epic checkpoint policy exactly."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

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


def _select_loopback_provider(model: str, source: dict[str, Any]) -> dict[str, Any]:
    try:
        provider_name, model_name = model.split("/", maxsplit=1)
        provider = source["provider"][provider_name]
        selected_model = provider["models"][model_name]
        options = provider["options"]
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("configured model is missing from the provider source") from error
    if not all(isinstance(value, dict) for value in (provider, selected_model, options)):
        raise RuntimeError("provider configuration must contain objects")
    if provider.get("npm") != "@ai-sdk/openai-compatible":
        raise RuntimeError("checkpoint provider must use the OpenAI-compatible adapter")
    if not set(provider) <= {"npm", "name", "options", "models"}:
        raise RuntimeError("checkpoint provider contains unsupported fields")
    if not set(options) <= {"baseURL", "timeout", "apiKey"}:
        raise RuntimeError("checkpoint provider options contain unsupported fields")
    if options.get("apiKey") != "local":
        raise RuntimeError("checkpoint provider must use the credentialless local sentinel")
    endpoint = urlparse(options.get("baseURL", ""))
    try:
        endpoint_port = endpoint.port
    except ValueError as error:
        raise RuntimeError("checkpoint provider endpoint is malformed") from error
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
        or endpoint_port is None
        or not 1 <= endpoint_port <= 65535
        or endpoint.username is not None
        or endpoint.password is not None
    ):
        raise RuntimeError("checkpoint provider endpoint must be loopback HTTP")
    timeout = options.get("timeout")
    if type(timeout) is not int or not 1 <= timeout <= 900_000:
        raise RuntimeError("checkpoint provider timeout is invalid")
    if not set(selected_model) <= {
        "name",
        "reasoning",
        "temperature",
        "tool_call",
        "limit",
    }:
        raise RuntimeError("checkpoint model contains unsupported fields")
    selected_provider = copy.deepcopy(provider)
    selected_provider["models"] = {model_name: copy.deepcopy(selected_model)}
    return {provider_name: selected_provider}


def render_config(
    model: str | None = None, provider_source: dict[str, Any] | None = None
) -> dict[str, Any]:
    config: dict[str, Any] = {
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
    if model is not None or provider_source is not None:
        if model is None or provider_source is None:
            raise RuntimeError("model and provider source must be supplied together")
        config["provider"] = _select_loopback_provider(model, provider_source)
        config["model"] = model
        config["small_model"] = model
    return config


def verify(
    config: dict[str, Any],
    model: str | None = None,
    provider_source: dict[str, Any] | None = None,
) -> None:
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
    expected = render_config(model, provider_source)
    for name in ("provider", "model", "small_model"):
        if config.get(name) != expected.get(name):
            _fail(f"{name} differs from the selected loopback provider")
    allowed_runtime_defaults = {"command", "mode", "username", "$schema", "plugin_origins"}
    unexpected = set(config) - set(expected) - allowed_runtime_defaults
    if unexpected:
        _fail(f"unexpected effective config fields: {sorted(unexpected)}")
    if config.get("command") not in (None, {}) or config.get("mode") not in (None, {}):
        _fail("runtime command or mode configuration remains")
    if "username" in config and not isinstance(config["username"], str):
        _fail("runtime username is malformed")


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
        if tools.get(name, False) is not False:
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
    parser.add_argument("--model")
    parser.add_argument("--provider-config", type=Path)
    args = parser.parse_args()
    try:
        provider_source = None
        if args.provider_config is not None:
            provider_source = json.loads(args.provider_config.read_text(encoding="utf-8"))
            if not isinstance(provider_source, dict):
                raise RuntimeError("provider source is not an object")
        if args.render_config:
            print(json.dumps(render_config(args.model, provider_source), separators=(",", ":")))
            return 0
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
            verify(config, args.model, provider_source)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"epic-worker-policy-check: failed: {error}")
        return 1
    print("epic-worker-policy-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
