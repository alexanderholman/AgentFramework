#!/usr/bin/env python3
"""Preview or run repeatable AF development commands without shell interpolation."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ENV_BIN = PROJECT_ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")

LOCKED_RUN = (
    "uv",
    "run",
    "--no-config",
    "--no-env-file",
    "--offline",
    "--locked",
    "--no-sources",
    "--extra",
    "dev",
)
LOCKED_ACTIONS = frozenset(
    {
        "check",
        "package",
        "release-check",
        "release-report",
        "release-report-json",
        "release-validate",
    }
)

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
    "release-validate": (
        (*LOCKED_RUN, "python", "scripts/release_gates.py", "--action", "validate"),
    ),
    "release-report": ((*LOCKED_RUN, "python", "scripts/release_gates.py", "--action", "report"),),
    "release-report-json": (
        (*LOCKED_RUN, "python", "scripts/release_gates.py", "--action", "report", "--json"),
    ),
    "release-check": ((*LOCKED_RUN, "python", "scripts/release_gates.py", "--action", "check"),),
    "package": (
        (
            *LOCKED_RUN,
            "python",
            "scripts/release_gates.py",
            "--action",
            "package",
            "--execute",
        ),
    ),
    "check": (
        (*LOCKED_RUN, "python", "scripts/release_gates.py", "--action", "validate"),
        (*LOCKED_RUN, "ruff", "format", "--check", "src/af", "tests", "scripts"),
        (*LOCKED_RUN, "ruff", "check", "src/af", "tests", "scripts"),
        (*LOCKED_RUN, "mypy", "src"),
        (
            *LOCKED_RUN,
            "pytest",
            "--cov=af",
            "--cov-report=term-missing",
            "--cov-fail-under=90",
        ),
        ("node", "--test", "tests/opencode/route-gate.test.mjs"),
        (
            *LOCKED_RUN,
            "python",
            "scripts/release_gates.py",
            "--action",
            "package",
            "--execute",
        ),
        ("./validate_agents.sh",),
    ),
}


def render(command: Sequence[str]) -> str:
    """Render argv safely for operator inspection."""
    return shlex.join(command)


def resolve_locked_uv_executable() -> str:
    """Resolve the uv executable from the inherited PATH for locked release actions."""
    inherited_path = os.environ.get("PATH", "")
    executable = shutil.which("uv", path=inherited_path)
    if executable is None:
        raise RuntimeError("locked release actions require uv on inherited PATH")
    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("locked release actions require an accessible uv executable") from exc
    if resolved.is_relative_to(PROJECT_ROOT / ".venv"):
        raise RuntimeError("locked release actions require uv outside the project .venv")
    return str(resolved)


def _is_nested_release_package_command(command: Sequence[str]) -> bool:
    """Return true when argv represents the nested release package invocation."""
    if len(command) < len(LOCKED_RUN):
        return False
    if tuple(command[1 : len(LOCKED_RUN)]) != LOCKED_RUN[1:]:
        return False
    try:
        script_index = command.index("scripts/release_gates.py")
    except ValueError:
        return False
    if script_index < 1 or command[script_index - 1] != "python":
        return False
    tail = command[script_index + 1 :]
    try:
        action_index = tail.index("--action")
    except ValueError:
        return False
    return (
        action_index + 1 < len(tail) and tail[action_index + 1] == "package" and "--execute" in tail
    )


def pin_locked_uv_commands(
    commands: tuple[tuple[str, ...], ...], uv_executable: str
) -> tuple[tuple[str, ...], ...]:
    """Pin uv invocations in locked commands to a resolved absolute executable path."""
    pinned: list[tuple[str, ...]] = []
    for command in commands:
        updated = command
        if command and command[0] == "uv":
            updated = (uv_executable, *command[1:])
        if _is_nested_release_package_command(updated) and "--uv" not in updated:
            updated = (*updated, "--uv", uv_executable)
        pinned.append(updated)
    return tuple(pinned)


def execution_environment(action: str) -> dict[str, str]:
    """Return a bounded environment for locked release actions."""
    if action not in LOCKED_ACTIONS:
        environment = os.environ.copy()
        environment.setdefault("UV_CACHE_DIR", str(Path(".cache/uv").resolve()))
        return environment

    environment = {
        name: value
        for name in ("HOME", "LANG", "LC_ALL", "PATH", "SSL_CERT_FILE")
        if (value := os.environ.get(name))
    }
    environment.update(
        {name: value for name, value in os.environ.items() if name.startswith("AF_APPEND_ONLY_")}
    )
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "UV_CACHE_DIR": str(Path(".cache/uv").resolve()),
            "UV_LOCKED": "1",
            "UV_NO_CONFIG": "1",
            "UV_NO_PROGRESS": "1",
            "UV_NO_SOURCES": "1",
            "UV_OFFLINE": "1",
            "UV_PYTHON": sys.executable,
        }
    )
    inherited_path = environment.get("PATH", "")
    environment["PATH"] = os.pathsep.join(
        part for part in (str(PROJECT_ENV_BIN), inherited_path) if part
    )
    return environment


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

    if args.action in LOCKED_ACTIONS:
        try:
            commands = pin_locked_uv_commands(commands, resolve_locked_uv_executable())
        except RuntimeError as exc:
            print(f"action '{args.action}' failed: {exc}", file=sys.stderr)
            return 2

    environment = execution_environment(args.action)

    for command in commands:
        completed = subprocess.run(command, check=False, env=environment)
        if completed.returncode:
            return completed.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
