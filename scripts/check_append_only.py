#!/usr/bin/env python3
"""Validate that governed files are preserved as append-only Git blobs."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

APPEND_ONLY_FILES = ("specs.md", "decisions.md", "agent_runs.md")
NULL_GIT_SHA = "0" * 40
MERGE_BASE_STRATEGY = "merge-base"
EXACT_STRATEGY = "exact"
# Reviewed bootstrap anchor for the first PR topology where the base predates
# the governed files. Its frozen entries below are part of the governance proof.
BOOTSTRAP_ANCHOR = "5457fcdd70aa50162fb798ba5e0efefc89809ea2"


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    object_type: str
    object_id: str


@dataclass(frozen=True)
class FrozenBootstrapFile:
    mode: str
    blob_id: str
    # Hex digest of the frozen blob bytes, checked in addition to the Git object ID.
    sha256: str


@dataclass(frozen=True)
class BlobReference:
    revision: str
    entry: TreeEntry
    content: bytes


@dataclass(frozen=True)
class BaseResolution:
    revision: str | None
    failure: str | None = None
    use_bootstrap_topology: bool = False


# Frozen reviewed bootstrap state for the imported governance history. These
# digests must only change with an intentional governance update.
FROZEN_BOOTSTRAP_FILES: dict[str, FrozenBootstrapFile] = {
    "specs.md": FrozenBootstrapFile(
        mode="100644",
        blob_id="a9965b08d0b15ddb049dd139b15102b5c33b6997",
        sha256="7f6fb0b3c0d04548af567b07b7c43493c44736ba095aed52149f5529669d2ce0",
    ),
    "decisions.md": FrozenBootstrapFile(
        mode="100644",
        blob_id="dd53088ce16c53a2b233e98ce90be0e41c28c661",
        sha256="2ca5088096bc8f33c5c3d03c63157866ad44981662c080d683d4fd3f50b97072",
    ),
    "agent_runs.md": FrozenBootstrapFile(
        mode="100644",
        blob_id="a2f49fdd983e15e79a6cb69e5907fa0c9c04c7da",
        sha256="6602d0982b2a81a86527ed911822b086d453c3016da548398ab9d8a086437454",
    ),
}


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


def _resolve_base_strategy(explicit: str | None) -> str:
    if explicit:
        return explicit
    environment_strategy = os.environ.get("AF_APPEND_ONLY_BASE_STRATEGY", "").strip()
    if environment_strategy:
        return environment_strategy
    return MERGE_BASE_STRATEGY


def _resolve_head_ref(explicit: str | None) -> str:
    if explicit:
        return explicit
    environment_ref = os.environ.get("AF_APPEND_ONLY_HEAD_REF", "").strip()
    if environment_ref:
        return environment_ref
    return "HEAD"


def _resolve_commit(root: Path, revision: str) -> str | None:
    completed = _git(root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _resolve_head_revision(root: Path, head_ref: str) -> tuple[str | None, str | None]:
    if _is_all_zero_ref(head_ref):
        return None, f"append-only-check: invalid head revision {head_ref}"
    commit = _resolve_commit(root, head_ref)
    if commit is None:
        return None, f"append-only-check: failed to resolve head revision for {head_ref}"
    current_head = _resolve_commit(root, "HEAD")
    if current_head is None:
        return None, "append-only-check: failed to resolve checked-out HEAD"
    if commit != current_head:
        message = (
            "append-only-check: checked-out HEAD "
            f"{current_head} does not match expected head {commit}"
        )
        return (
            None,
            message,
        )
    return commit, None


def _resolve_base_revision(
    root: Path,
    base_ref: str,
    strategy: str,
    *,
    head_revision: str,
    head_ref: str,
) -> BaseResolution:
    if strategy == EXACT_STRATEGY:
        if _is_all_zero_ref(base_ref):
            return BaseResolution(revision=None, use_bootstrap_topology=True)
        commit = _resolve_commit(root, base_ref)
        if commit is None:
            return BaseResolution(
                revision=None,
                failure=f"append-only-check: failed to resolve base revision for {base_ref}",
            )
        if not _is_ancestor(root, commit, head_revision):
            return BaseResolution(
                revision=None,
                failure=f"append-only-check: push base {commit} is not an ancestor of {head_ref}",
            )
        return BaseResolution(revision=commit)
    completed = _git(root, "merge-base", head_revision, base_ref)
    if completed.returncode != 0:
        return BaseResolution(
            revision=None,
            failure=f"append-only-check: failed to resolve base revision for {base_ref}",
        )
    return BaseResolution(revision=completed.stdout.strip())


def _is_all_zero_ref(value: str) -> bool:
    return value == NULL_GIT_SHA


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = _git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    return completed.returncode == 0


def _load_tree_entry(root: Path, revision: str, relative_path: str) -> TreeEntry | None:
    """Return the Git tree entry for a path at a revision, or None when absent."""
    completed = _git(root, "ls-tree", revision, "--", relative_path)
    if completed.returncode != 0:
        return None
    line = completed.stdout.strip()
    if not line:
        return None
    metadata, actual_path = line.split("\t", maxsplit=1)
    if actual_path != relative_path:
        return None
    mode, object_type, object_id = metadata.split(" ", maxsplit=2)
    return TreeEntry(mode=mode, object_type=object_type, object_id=object_id)


def _load_blob_bytes(root: Path, object_id: str) -> bytes:
    """Return raw Git blob bytes, raising ValueError when the object is missing."""
    completed = _git(root, "cat-file", "blob", object_id, text=False)
    if completed.returncode != 0:
        raise ValueError(f"missing git blob {object_id}")
    return completed.stdout


def _validate_bootstrap_anchor(
    root: Path,
    base_revision: str | None,
) -> tuple[dict[str, BlobReference], list[str]]:
    """Validate the reviewed bootstrap anchor used when base predates governed files."""
    failures: list[str] = []
    if base_revision is not None and not _is_ancestor(root, base_revision, BOOTSTRAP_ANCHOR):
        failures.append(
            "bootstrap anchor "
            f"{BOOTSTRAP_ANCHOR} is not on the reviewed lineage after {base_revision}"
        )
        return {}, failures
    if not _is_ancestor(root, BOOTSTRAP_ANCHOR, "HEAD"):
        failures.append(f"bootstrap anchor {BOOTSTRAP_ANCHOR} is not on the HEAD lineage")
        return {}, failures

    references: dict[str, BlobReference] = {}
    for relative_path, frozen in FROZEN_BOOTSTRAP_FILES.items():
        entry = _load_tree_entry(root, BOOTSTRAP_ANCHOR, relative_path)
        if entry is None:
            failures.append(f"{relative_path}: bootstrap anchor {BOOTSTRAP_ANCHOR} is missing")
            continue
        if (
            entry.object_type != "blob"
            or entry.mode != frozen.mode
            or entry.object_id != frozen.blob_id
        ):
            failures.append(
                f"{relative_path}: bootstrap anchor {BOOTSTRAP_ANCHOR} "
                "does not match the frozen blob"
            )
            continue
        content = _load_blob_bytes(root, entry.object_id)
        if hashlib.sha256(content).hexdigest() != frozen.sha256:
            failures.append(
                f"{relative_path}: bootstrap anchor {BOOTSTRAP_ANCHOR} "
                "does not match the frozen digest"
            )
            continue
        references[relative_path] = BlobReference(
            revision=BOOTSTRAP_ANCHOR,
            entry=entry,
            content=content,
        )
    return references, failures


def _resolve_references(
    root: Path,
    base_revision: str,
) -> tuple[dict[str, BlobReference], list[str]]:
    """Resolve append-only references from base history or the reviewed bootstrap anchor."""
    base_entries = {
        relative_path: _load_tree_entry(root, base_revision, relative_path)
        for relative_path in APPEND_ONLY_FILES
    }
    missing_paths = [path for path, entry in base_entries.items() if entry is None]
    if not missing_paths:
        references: dict[str, BlobReference] = {}
        for relative_path in APPEND_ONLY_FILES:
            entry = base_entries[relative_path]
            assert entry is not None
            references[relative_path] = BlobReference(
                revision=base_revision,
                entry=entry,
                content=_load_blob_bytes(root, entry.object_id),
            )
        return references, []
    if len(missing_paths) != len(APPEND_ONLY_FILES):
        return {}, [
            f"base revision {base_revision} inconsistently contains append-only files: "
            + ", ".join(sorted(missing_paths))
        ]
    return _validate_bootstrap_anchor(root, base_revision)


def _is_regular_non_symlink(path: Path) -> bool:
    """Return True only for existing regular worktree files that are not symlinks."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    return stat.S_ISREG(mode) and not path.is_symlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate append-only governance files.")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--base-ref", default=None, help="Base ref or commit to compare against")
    parser.add_argument(
        "--base-strategy",
        choices=(MERGE_BASE_STRATEGY, EXACT_STRATEGY),
        default=None,
        help="Whether to resolve the base ref by merge-base or as an exact ancestor",
    )
    parser.add_argument(
        "--head-ref",
        default=None,
        help="Exact checked-out head revision expected for validation",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    base_ref = _resolve_base_ref(root, args.base_ref)
    if base_ref is None:
        print("append-only-check: skipped (no git base ref available)")
        return 0

    base_strategy = _resolve_base_strategy(args.base_strategy)
    head_ref = _resolve_head_ref(args.head_ref)
    head_revision, head_failure = _resolve_head_revision(root, head_ref)
    if head_failure is not None:
        print(head_failure)
        return 1
    assert head_revision is not None
    base_resolution = _resolve_base_revision(
        root,
        base_ref,
        base_strategy,
        head_revision=head_revision,
        head_ref=head_ref,
    )
    if base_resolution.failure is not None:
        print(base_resolution.failure)
        return 1

    if base_resolution.use_bootstrap_topology:
        references, failures = _validate_bootstrap_anchor(root, None)
    else:
        assert base_resolution.revision is not None
        references, failures = _resolve_references(root, base_resolution.revision)
    for relative_path in APPEND_ONLY_FILES:
        reference = references.get(relative_path)
        if reference is None:
            continue
        head_entry = _load_tree_entry(root, "HEAD", relative_path)
        if head_entry is None:
            failures.append(f"{relative_path}: file is missing from the HEAD tree")
            continue
        if head_entry.object_type != "blob":
            failures.append(f"{relative_path}: HEAD tree entry is not a blob")
            continue
        if head_entry.mode != reference.entry.mode:
            failures.append(
                f"{relative_path}: HEAD mode {head_entry.mode} does not match {reference.revision}"
            )
            continue
        head_bytes = _load_blob_bytes(root, head_entry.object_id)
        if not head_bytes.startswith(reference.content):
            failures.append(
                f"{relative_path}: HEAD blob is not a pure append of {reference.revision}"
            )
            continue
        current_path = root / relative_path
        if not _is_regular_non_symlink(current_path):
            failures.append(
                f"{relative_path}: working tree path must be a regular non-symlink file"
            )
            continue
        current_bytes = current_path.read_bytes()
        if current_bytes != head_bytes:
            failures.append(f"{relative_path}: working tree content does not match HEAD")

    if failures:
        print("append-only-check: failed")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print("append-only-check: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
