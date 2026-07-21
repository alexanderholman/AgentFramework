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
    if _run(launcher, "--version").strip() != EXPECTED_VERSION:
        raise RuntimeError(f"OpenCode version must be {EXPECTED_VERSION}")

    paths = _run(launcher, "debug", "paths")
    for line in paths.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        candidate = Path(parts[1]).resolve()
        if not candidate.is_relative_to(root):
            raise RuntimeError(f"path escapes isolated root: {candidate}")

    config_text = _run(launcher, "debug", "config")
    _assert_clean(config_text)
    config = json.loads(config_text)
    if config.get("default_agent") != "dispatcher":
        raise RuntimeError("dispatcher is not the default agent")
    agents = set(config.get("agent", {}))
    if not agents >= EXPECTED_CUSTOM_AGENTS or not agents <= ALLOWED_AGENT_KEYS:
        raise RuntimeError(f"unexpected configured agents: {sorted(agents)}")
    origins = config.get("plugin_origins", [])
    if len(origins) != 1 or not str(origins[0].get("spec", "")).endswith("/plugins/af-route.js"):
        raise RuntimeError("exactly one AF route plugin must be loaded")

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
