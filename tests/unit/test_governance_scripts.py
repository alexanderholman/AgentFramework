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
    ci: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(APPEND_ONLY_SCRIPT), "--root", str(root)]
    if base_ref is not None:
        command.extend(["--base-ref", base_ref])
    environment = dict(environ)
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


def test_python_workflow_uses_pr_base_sha_without_fetching_origin() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "python.yml").read_text(encoding="utf-8")

    assert "github.event.pull_request.base.sha" in workflow
    assert "git fetch --no-tags origin" not in workflow
