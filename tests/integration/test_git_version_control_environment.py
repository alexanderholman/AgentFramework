from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

import pytest

from af.framework.version_control import InitializationStatus, VersionControlRequest
from af.infrastructure.git_version_control import GitVersionControlBackend


def _git(cwd: Path, *arguments: str) -> str:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_EMAIL": "af-tests@example.invalid",
            "GIT_AUTHOR_NAME": "AF Tests",
            "GIT_COMMITTER_EMAIL": "af-tests@example.invalid",
            "GIT_COMMITTER_NAME": "AF Tests",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    completed = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--initial-branch=main")
    (repository / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    _git(repository, "remote", "add", "origin", "https://example.invalid/repository.git")
    return repository


@pytest.mark.skipif(os.name == "nt", reason="sentinel helper uses a POSIX executable script")
def test_initializer_ignores_ambient_git_exec_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    trusted_exec_path = Path(_git(repository, "--exec-path"))
    trusted_receive_pack = trusted_exec_path / "git-receive-pack"
    assert trusted_receive_pack.is_file()

    sentinel = tmp_path / "ambient-helper-executed"
    helper_directory = tmp_path / "untrusted-git-exec-path"
    helper_directory.mkdir()
    helper = helper_directory / "git-receive-pack"
    helper.write_text(
        "#!/bin/sh\n"
        f"printf invoked > {shlex.quote(str(sentinel))}\n"
        f'exec {shlex.quote(str(trusted_receive_pack))} "$@"\n',
        encoding="utf-8",
    )
    helper.chmod(0o700)
    monkeypatch.setenv("GIT_EXEC_PATH", str(helper_directory))

    result = GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))

    assert result.status is InitializationStatus.INITIALIZED
    assert not sentinel.exists()
    assert Path(result.manifest.coordination.repository_path).is_dir()
