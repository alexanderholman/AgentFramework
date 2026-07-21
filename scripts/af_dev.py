#!/usr/bin/env python3
"""Preview or run repeatable AF development commands without shell interpolation."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

ACTIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "sync": (("uv", "sync", "--locked", "--extra", "dev"),),
    "test": (("uv", "run", "pytest"),),
    "coverage": (
        ("uv", "run", "pytest", "--cov=af", "--cov-report=term-missing", "--cov-fail-under=90"),
    ),
    "smoke": (
        ("uv", "run", "af", "hello"),
        ("uv", "run", "af", "ask", "how many rs in strawberry"),
    ),
    "lint": (("uv", "run", "ruff", "check", "src/af", "tests", "scripts"),),
    "lint-fix": (("uv", "run", "ruff", "check", "--fix", "src/af", "tests", "scripts"),),
    "format": (("uv", "run", "ruff", "format", "src/af", "tests", "scripts"),),
    "format-check": (("uv", "run", "ruff", "format", "--check", "src/af", "tests", "scripts"),),
    "typecheck": (("uv", "run", "mypy", "src"),),
    "legacy": (("./validate_agents.sh",),),
    "opencode-integration": (("node", "--test", "tests/opencode/route-gate.test.mjs"),),
    "check": (
        ("uv", "run", "ruff", "format", "--check", "src/af", "tests", "scripts"),
        ("uv", "run", "ruff", "check", "src/af", "tests", "scripts"),
        ("uv", "run", "mypy", "src"),
        ("uv", "run", "pytest", "--cov=af", "--cov-report=term-missing", "--cov-fail-under=90"),
        ("node", "--test", "tests/opencode/route-gate.test.mjs"),
        ("./validate_agents.sh",),
    ),
}


def render(command: Sequence[str]) -> str:
    """Render argv safely for operator inspection."""
    return shlex.join(command)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=sorted(ACTIONS), required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run the command; without this flag only print the command plan.",
    )
    args = parser.parse_args()

    commands = ACTIONS[args.action]
    for command in commands:
        print(render(command))

    if not args.execute:
        print("Dry run only. Re-run with --execute to apply.")
        return 0

    environment = os.environ.copy()
    environment.setdefault("UV_CACHE_DIR", str(Path(".cache/uv").resolve()))

    for command in commands:
        completed = subprocess.run(command, check=False, env=environment)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
