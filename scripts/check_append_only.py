#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

APPEND_ONLY_FILES = ("specs.md", "decisions.md", "agent_runs.md")


def _git(
    root: Path,
    *args: str,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        check=False,
        text=text,
    )


def _resolve_base_ref(root: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    environment_ref = os.environ.get("AF_APPEND_ONLY_BASE_REF", "").strip()
    if environment_ref:
        return environment_ref
    completed = _git(root, "rev-parse", "--verify", "HEAD~1")
    if completed.returncode == 0:
        return "HEAD~1"
    return None


def _resolve_base_revision(root: Path, base_ref: str) -> str | None:
    completed = _git(root, "merge-base", "HEAD", base_ref)
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate append-only governance files.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--base-ref", default=None, help="Base ref or commit to compare against")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    base_ref = _resolve_base_ref(root, args.base_ref)
    if base_ref is None:
        print("append-only-check: skipped (no git base ref available)")
        return 0

    base_revision = _resolve_base_revision(root, base_ref)
    if base_revision is None:
        print(f"append-only-check: failed to resolve base revision for {base_ref}")
        return 1

    failures: list[str] = []
    for relative_path in APPEND_ONLY_FILES:
        current_path = root / relative_path
        if not current_path.exists():
            failures.append(f"{relative_path}: file is missing in the working tree")
            continue
        current_bytes = current_path.read_bytes()
        base_file = _git(root, "show", f"{base_revision}:{relative_path}", text=False)
        if base_file.returncode != 0:
            failures.append(f"{relative_path}: file is missing in base revision {base_revision}")
            continue
        base_bytes = base_file.stdout
        if not current_bytes.startswith(base_bytes):
            failures.append(
                f"{relative_path}: current content is not a pure append of {base_revision}"
            )

    if failures:
        print("append-only-check: failed")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("append-only-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
