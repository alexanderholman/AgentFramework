#!/usr/bin/env python3
"""Verify an isolated OpenCode installation has AF routing and no MMPR contamination."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

EXPECTED_VERSION = "1.18.4"
EXPECTED_CUSTOM_AGENTS = {"architect", "builder", "dispatcher", "general", "reviewer", "tester"}
ALLOWED_AGENT_KEYS = EXPECTED_CUSTOM_AGENTS | {"build", "explore", "plan"}
ALLOWED_LISTED_AGENTS = EXPECTED_CUSTOM_AGENTS | {"compaction", "summary", "title"}
FORBIDDEN_MARKERS = ("mmpr", "power ranger", "pr-universe", "zordon", "/opt/pr/")
READ_ONLY_PERMISSIONS = {
    "*": "deny",
    "read": "allow",
    "glob": "allow",
    "grep": "allow",
    "list": "allow",
    "webfetch": "allow",
    "websearch": "allow",
    "question": "allow",
    "task": "deny",
    "skill": "deny",
}
EXPECTED_PERMISSIONS = {
    "dispatcher": {
        "*": "deny",
        "af_route": "allow",
        "question": "allow",
        "task": {
            "*": "deny",
            "architect": "allow",
            "builder": "allow",
            "reviewer": "allow",
            "tester": "allow",
            "general": "allow",
        },
        "skill": "deny",
    },
    "architect": READ_ONLY_PERMISSIONS,
    "general": READ_ONLY_PERMISSIONS,
    "builder": {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "edit": "allow",
        "bash": "deny",
        "question": "allow",
        "task": "deny",
        "skill": "deny",
    },
    "reviewer": {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "bash": "deny",
        "edit": "deny",
        "task": "deny",
        "skill": "deny",
    },
    "tester": {
        "*": "deny",
        "read": "allow",
        "glob": "allow",
        "grep": "allow",
        "list": "allow",
        "bash": "deny",
        "edit": "deny",
        "task": "deny",
        "skill": "deny",
    },
}


def _run(launcher: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [str(launcher), *arguments], capture_output=True, check=False, text=True
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "command failed")
    return completed.stdout


def _assert_clean(content: str) -> None:
    lowered = content.casefold()
    found = [marker for marker in FORBIDDEN_MARKERS if marker in lowered]
    if found:
        raise RuntimeError(f"forbidden contamination markers found: {', '.join(found)}")


def verify(root: Path, launcher: Path) -> None:
    root = root.resolve()
    if root == Path(root.anchor):
        raise RuntimeError("isolated root must not be the filesystem root")
    if _run(launcher, "--version").strip() != EXPECTED_VERSION:
        raise RuntimeError(f"OpenCode version must be {EXPECTED_VERSION}")

    paths = _run(launcher, "debug", "paths")
    expected_paths = {
        "home": root / "home",
        "data": root / "xdg/data/opencode",
        "cache": root / "xdg/cache/opencode",
        "config": root / "xdg/config/opencode",
        "state": root / "xdg/state/opencode",
        "tmp": root / "tmp/opencode",
    }
    observed_paths: dict[str, Path] = {}
    for line in paths.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise RuntimeError("debug paths output is malformed")
        label, value = parts
        if label not in expected_paths or label in observed_paths:
            raise RuntimeError(f"unexpected debug paths label: {label}")
        candidate = Path(value).resolve()
        if not candidate.is_relative_to(root):
            raise RuntimeError(f"path escapes isolated root: {candidate}")
        observed_paths[label] = candidate
    if observed_paths != {name: path.resolve() for name, path in expected_paths.items()}:
        raise RuntimeError("debug paths do not match the isolated layout")

    config_text = _run(launcher, "debug", "config")
    config = json.loads(config_text)
    if config.get("default_agent") != "dispatcher":
        raise RuntimeError("dispatcher is not the default agent")
    if config.get("share") != "disabled" or config.get("autoupdate") is not False:
        raise RuntimeError("sharing and automatic updates must remain disabled")
    agents = set(config.get("agent", {}))
    if not agents >= EXPECTED_CUSTOM_AGENTS or not agents <= ALLOWED_AGENT_KEYS:
        raise RuntimeError(f"unexpected configured agents: {sorted(agents)}")
    for name in ("build", "explore", "plan"):
        if config["agent"].get(name, {}).get("disable") is not True:
            raise RuntimeError(f"built-in agent is not disabled: {name}")
    for name, expected in EXPECTED_PERMISSIONS.items():
        if config["agent"].get(name, {}).get("permission") != expected:
            raise RuntimeError(f"unexpected permissions for {name}")
    origins = config.get("plugin_origins", [])
    expected_plugin = (root / "xdg/config/opencode/plugins/af-route.js").as_uri()
    if len(origins) != 1 or origins[0].get("spec") != expected_plugin:
        raise RuntimeError("exactly one AF route plugin must be loaded")
    expected_mcp_command = [str(root / "af-venv/bin/af-mcp")]
    try:
        mcp_command = config["mcp"]["af"]["command"]
    except (KeyError, TypeError):
        mcp_command = None
    if mcp_command != expected_mcp_command:
        raise RuntimeError("AF MCP command does not match the isolated root")
    config_for_scan = json.loads(config_text)
    config_for_scan["plugin_origins"] = [{"spec": "[isolated-root]/af-route.js"}]
    config_for_scan["mcp"]["af"]["command"] = ["[isolated-root]/af-mcp"]
    _assert_clean(json.dumps(config_for_scan))

    agent_text = _run(launcher, "agent", "list")
    _assert_clean(agent_text)
    listed = {
        line.split(" ", maxsplit=1)[0]
        for line in agent_text.splitlines()
        if line.endswith((" (primary)", " (subagent)"))
    }
    if listed != ALLOWED_LISTED_AGENTS:
        raise RuntimeError(f"unexpected effective agents: {sorted(listed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    args = parser.parse_args()
    try:
        verify(args.root, args.launcher)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"isolated-opencode-check: failed: {error}")
        return 1
    print("isolated-opencode-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
