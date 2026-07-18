from __future__ import annotations

import runpy
import subprocess
import sys
import tomllib
from collections.abc import Callable
from os import environ
from pathlib import Path
from typing import cast

import yaml

SCRIPT = Path(__file__).parents[2] / "scripts" / "check_repeatable_script_refs.py"
APPEND_ONLY_SCRIPT = Path(__file__).parents[2] / "scripts" / "check_append_only.py"
REPO_ROOT = Path(__file__).parents[2]
# Reviewed bootstrap anchor and first-PR base topology from this repository's
# imported governance history.
BOOTSTRAP_ANCHOR = "5457fcdd70aa50162fb798ba5e0efefc89809ea2"
FIRST_PR_BASE = "4e27a97f7887dfb4554ff2d74f8262c23deb25a4"
has_script_ref = cast(
    Callable[[list[str], int], bool], runpy.run_path(str(SCRIPT))["has_script_ref"]
)


def test_script_reference_five_lines_after_repeatable_statement_is_nearby() -> None:
    lines = [
        "Repeatable work must be scripted.",
        "detail one",
        "detail two",
        "detail three",
        "detail four",
        "Use scripts/registry.yaml.",
    ]

    assert has_script_ref(lines, 0) is True


def test_dev_dependencies_include_pyyaml_for_registry_scripts() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert any(dependency.lower().startswith("pyyaml") for dependency in dev_dependencies)
    assert all(not dependency.lower().startswith("httpx2") for dependency in dev_dependencies)


def test_validate_agents_uses_uv_managed_python_for_yaml_registry_checks() -> None:
    script = (REPO_ROOT / "validate_agents.sh").read_text(encoding="utf-8")

    assert "scripts/validate_registry.py --registry scripts/registry.yaml" in script
    assert "scripts/check_dry_run_conventions.py --registry scripts/registry.yaml" in script
    assert "scripts/check_append_only.py --root ." in script
    assert "eval " not in script


def test_validate_agents_does_not_eval_agent_filenames(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    sentinel = tmp_path / "sentinel.txt"
    agent_path = agents_dir / "safe 'quoted'; $(touch sentinel.txt); name.md"
    agent_path.write_text(
        "\n".join(
            [
                "## Purpose",
                "Test",
                "## Inputs",
                "Test",
                "## Outputs",
                "Test",
                "## Behavior",
                "Test",
                "## Constraints",
                "Test",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "agents.md").write_text("# Agents\n", encoding="utf-8")
    (tmp_path / "specs.md").write_text("specs\n", encoding="utf-8")
    (tmp_path / "decisions.md").write_text("decisions\n", encoding="utf-8")
    (tmp_path / "agent_runs.md").write_text("runs\n", encoding="utf-8")
    (tmp_path / "scripts").symlink_to(REPO_ROOT / "scripts", target_is_directory=True)
    (tmp_path / "agents.yaml").write_text(
        yaml.safe_dump(
            {
                "allowed_tags": ["test"],
                "agents": [
                    {
                        "id": "safe-agent",
                        "name": "Safe Agent",
                        "description": "Regression fixture",
                        "version": "1.0.0",
                        "tags": ["test"],
                        "file_path": f"agents/{agent_path.name}",
                        "status": "active",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        ["bash", str(REPO_ROOT / "validate_agents.sh")],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        text=True,
    )

    assert completed.returncode == 0
    assert sentinel.exists() is False


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_stdout(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_append_only_repo(tmp_path: Path) -> None:
    _git("init", "-b", "master", cwd=tmp_path)
    _git("config", "user.name", "Test User", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    for name in ("specs.md", "decisions.md", "agent_runs.md"):
        (tmp_path / name).write_text(f"{name}\nbase line\n", encoding="utf-8")
    _git("add", "specs.md", "decisions.md", "agent_runs.md", cwd=tmp_path)
    _git("commit", "-m", "base", cwd=tmp_path)


def _run_append_only(
    root: Path,
    *,
    base_ref: str | None = None,
    base_strategy: str | None = None,
    head_ref: str | None = None,
    ci: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the append-only script for a repo root with optional base-ref and CI mode."""
    command = [sys.executable, str(APPEND_ONLY_SCRIPT), "--root", str(root)]
    if base_ref is not None:
        command.extend(["--base-ref", base_ref])
    if head_ref is not None:
        command.extend(["--head-ref", head_ref])
    environment = {"PATH": environ.get("PATH", "")}
    if base_strategy is not None:
        environment["AF_APPEND_ONLY_BASE_STRATEGY"] = base_strategy
    if ci:
        environment["CI"] = "true"
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def test_append_only_check_allows_pure_appends(tmp_path: Path) -> None:
    _init_append_only_repo(tmp_path)
    (tmp_path / "specs.md").write_text("specs.md\nbase line\nnew line\n", encoding="utf-8")
    _git("add", "specs.md", cwd=tmp_path)
    _git("commit", "-m", "append specs", cwd=tmp_path)

    completed = _run_append_only(tmp_path, base_ref="HEAD~1")

    assert completed.returncode == 0
    assert "append-only-check: ok" in completed.stdout


def test_append_only_check_rejects_non_append_edits(tmp_path: Path) -> None:
    _init_append_only_repo(tmp_path)
    (tmp_path / "specs.md").write_text("specs.md\nedited line\n", encoding="utf-8")

    completed = _run_append_only(tmp_path, base_ref="HEAD")

    assert completed.returncode == 1
    assert "specs.md" in completed.stdout


def test_append_only_check_has_explicit_local_skip_without_git_context(tmp_path: Path) -> None:
    for name in ("specs.md", "decisions.md", "agent_runs.md"):
        (tmp_path / name).write_text(f"{name}\n", encoding="utf-8")

    completed = _run_append_only(tmp_path)

    assert completed.returncode == 0
    assert "append-only-check: skipped" in completed.stdout


def test_append_only_check_uses_bootstrap_anchor_for_first_pr_topology() -> None:
    # This intentionally exercises the repository's reviewed first-PR topology:
    # the real base predates the governed files and must fall back to the anchor.
    completed = _run_append_only(REPO_ROOT, base_ref=FIRST_PR_BASE, ci=True)

    assert completed.returncode == 0
    assert "append-only-check: ok" in completed.stdout


def test_append_only_check_fails_when_ci_history_lacks_bootstrap_anchor(tmp_path: Path) -> None:
    _git("init", "-b", "master", cwd=tmp_path)
    _git("config", "user.name", "Test User", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("commit", "--allow-empty", "-m", "base", cwd=tmp_path)
    for name in ("specs.md", "decisions.md", "agent_runs.md"):
        (tmp_path / name).write_text(f"{name}\nanchor line\nhead line\n", encoding="utf-8")
    _git("add", "specs.md", "decisions.md", "agent_runs.md", cwd=tmp_path)
    _git("commit", "-m", "head", cwd=tmp_path)

    completed = _run_append_only(tmp_path, base_ref="HEAD~1", ci=True)

    assert completed.returncode == 1
    assert BOOTSTRAP_ANCHOR in completed.stdout


def test_append_only_check_catches_multi_commit_push_mutation_from_before_sha(
    tmp_path: Path,
) -> None:
    _init_append_only_repo(tmp_path)
    before_sha = _git_stdout("rev-parse", "HEAD", cwd=tmp_path)
    (tmp_path / "specs.md").write_text("specs.md\nmutated line\n", encoding="utf-8")
    _git("add", "specs.md", cwd=tmp_path)
    _git("commit", "-m", "mutate governed line", cwd=tmp_path)
    (tmp_path / "specs.md").write_text(
        "specs.md\nmutated line\nappended after mutation\n",
        encoding="utf-8",
    )
    _git("add", "specs.md", cwd=tmp_path)
    _git("commit", "-m", "append after mutation", cwd=tmp_path)

    completed = _run_append_only(tmp_path, base_ref=before_sha, base_strategy="exact", ci=True)

    assert completed.returncode == 1
    assert "specs.md: HEAD blob is not a pure append" in completed.stdout


def test_append_only_check_rejects_non_ancestor_push_before_sha(tmp_path: Path) -> None:
    _init_append_only_repo(tmp_path)
    before_sha = _git_stdout("rev-parse", "HEAD", cwd=tmp_path)
    _git("checkout", "-b", "previous-tip", cwd=tmp_path)
    (tmp_path / "specs.md").write_text("specs.md\nbase line\nold branch append\n", encoding="utf-8")
    _git("add", "specs.md", cwd=tmp_path)
    _git("commit", "-m", "old branch append", cwd=tmp_path)
    before_sha = _git_stdout("rev-parse", "HEAD", cwd=tmp_path)
    _git("checkout", "master", cwd=tmp_path)
    (tmp_path / "specs.md").write_text("specs.md\nbase line\nnew branch append\n", encoding="utf-8")
    _git("add", "specs.md", cwd=tmp_path)
    _git("commit", "-m", "new branch append", cwd=tmp_path)

    completed = _run_append_only(tmp_path, base_ref=before_sha, base_strategy="exact", ci=True)

    assert completed.returncode == 1
    assert f"push base {before_sha} is not an ancestor of HEAD" in completed.stdout


def test_append_only_check_rejects_checked_out_head_that_differs_from_push_after_sha(
    tmp_path: Path,
) -> None:
    _init_append_only_repo(tmp_path)
    before_sha = _git_stdout("rev-parse", "HEAD", cwd=tmp_path)
    (tmp_path / "specs.md").write_text("specs.md\nbase line\nold pushed append\n", encoding="utf-8")
    _git("add", "specs.md", cwd=tmp_path)
    _git("commit", "-m", "old pushed append", cwd=tmp_path)
    pushed_after_sha = _git_stdout("rev-parse", "HEAD", cwd=tmp_path)
    _git("checkout", "-b", "replacement", before_sha, cwd=tmp_path)
    (tmp_path / "specs.md").write_text(
        "specs.md\nbase line\nreplacement append\n", encoding="utf-8"
    )
    _git("add", "specs.md", cwd=tmp_path)
    _git("commit", "-m", "replacement append", cwd=tmp_path)

    completed = _run_append_only(
        tmp_path,
        base_ref=before_sha,
        base_strategy="exact",
        head_ref=pushed_after_sha,
        ci=True,
    )

    assert completed.returncode == 1
    assert "does not match expected head" in completed.stdout


def test_append_only_check_rejects_all_zero_push_after_sha(tmp_path: Path) -> None:
    _init_append_only_repo(tmp_path)

    completed = _run_append_only(
        tmp_path,
        base_ref="HEAD",
        base_strategy="exact",
        head_ref="0" * 40,
        ci=True,
    )

    assert completed.returncode == 1
    assert "invalid head revision" in completed.stdout


def test_append_only_check_rejects_all_zero_push_before_sha_without_bootstrap_lineage(
    tmp_path: Path,
) -> None:
    _git("init", "-b", "master", cwd=tmp_path)
    _git("config", "user.name", "Test User", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("commit", "--allow-empty", "-m", "base", cwd=tmp_path)
    for name in ("specs.md", "decisions.md", "agent_runs.md"):
        (tmp_path / name).write_text(f"{name}\nanchor line\nhead line\n", encoding="utf-8")
    _git("add", "specs.md", "decisions.md", "agent_runs.md", cwd=tmp_path)
    _git("commit", "-m", "head", cwd=tmp_path)

    completed = _run_append_only(tmp_path, base_ref="0" * 40, base_strategy="exact", ci=True)

    assert completed.returncode == 1
    assert BOOTSTRAP_ANCHOR in completed.stdout


def test_append_only_check_fails_when_worktree_differs_from_head_blob(tmp_path: Path) -> None:
    _init_append_only_repo(tmp_path)
    for name in ("specs.md", "decisions.md", "agent_runs.md"):
        path = tmp_path / name
        path.write_text(f"{name}\nbase line\nhead line\n", encoding="utf-8")
    _git("add", "specs.md", "decisions.md", "agent_runs.md", cwd=tmp_path)
    _git("commit", "-m", "append", cwd=tmp_path)
    (tmp_path / "specs.md").write_text(
        "specs.md\nbase line\nhead line\ntest mutation\n",
        encoding="utf-8",
    )

    completed = _run_append_only(tmp_path, base_ref="HEAD~1")

    assert completed.returncode == 1
    assert "specs.md: working tree content does not match HEAD" in completed.stdout


def test_append_only_check_fails_when_head_changes_append_only_mode_to_symlink(
    tmp_path: Path,
) -> None:
    _init_append_only_repo(tmp_path)
    for name in ("specs.md", "decisions.md", "agent_runs.md"):
        path = tmp_path / name
        path.unlink()
        path.symlink_to("other.txt")
    _git("add", "specs.md", "decisions.md", "agent_runs.md", cwd=tmp_path)
    _git("commit", "-m", "replace with symlink", cwd=tmp_path)

    completed = _run_append_only(tmp_path, base_ref="HEAD~1")

    assert completed.returncode == 1
    assert "specs.md: HEAD mode 120000 does not match " in completed.stdout


def test_python_workflow_uses_pr_merge_base_and_push_exact_base_sha() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "python.yml").read_text(encoding="utf-8")

    assert "github.event.pull_request.base.sha" in workflow
    assert "github.event.before" in workflow
    assert "github.event.after" in workflow
    assert "AF_APPEND_ONLY_BASE_STRATEGY" in workflow
    assert "github.event_name == 'push' && 'exact' || 'merge-base'" in workflow
    assert "AF_APPEND_ONLY_HEAD_REF" in workflow
    assert "Ensure push before and after commits are available" in workflow
