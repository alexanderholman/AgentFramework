from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from af.framework.version_control import (
    GitObjectFormat,
    InitializationStatus,
    VersionControlInitializationError,
    VersionControlRequest,
)
from af.infrastructure import git_version_control as git_version_control_module
from af.infrastructure.git_version_control import GitVersionControlBackend


def _git(cwd: Path, *arguments: str, check: bool = True) -> str:
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
        check=check,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed.stdout.strip()


def _repository(
    tmp_path: Path,
    *,
    name: str = "repository",
    branch: str = "main",
    object_format: str = "sha1",
) -> Path:
    repository = tmp_path / name
    repository.mkdir()
    _git(
        repository,
        "init",
        f"--initial-branch={branch}",
        f"--object-format={object_format}",
    )
    (repository / "README.md").write_text("# fixture\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial")
    return repository


def test_initializer_establishes_idempotent_local_coordination_remote(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    upstream_url = (
        "https://operator:do-not-record@example.invalid/owner/repository.git?token=secret"
    )
    _git(repository, "remote", "add", "origin", upstream_url)
    head = _git(repository, "rev-parse", "HEAD")
    backend = GitVersionControlBackend()

    first = backend.initialize(VersionControlRequest(cwd=repository))

    assert first.status is InitializationStatus.INITIALIZED
    assert first.manifest.repository.root == str(repository.resolve())
    assert first.manifest.repository.branch == "main"
    assert first.manifest.repository.head_sha == head
    assert first.manifest.upstream_remote.name == "origin"
    assert first.manifest.upstream_remote.fetch_url == (
        "https://example.invalid/owner/repository.git"
    )
    assert first.manifest.upstream_remote.push_url == (
        "https://example.invalid/owner/repository.git"
    )
    assert _git(repository, "remote", "get-url", "origin") == upstream_url
    assert _git(repository, "remote", "get-url", "af-local") == (
        first.manifest.coordination.repository_path
    )

    coordination = Path(first.manifest.coordination.repository_path)
    assert _git(
        repository, "--git-dir", str(coordination), "rev-parse", "--is-bare-repository"
    ) == ("true")
    assert (
        _git(
            repository,
            "--git-dir",
            str(coordination),
            "rev-parse",
            "refs/heads/main",
        )
        == head
    )
    assert Path(first.manifest.coordination.worktree_root).is_dir()

    manifest_path = Path(first.manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    assert manifest_bytes == first.manifest.file_bytes()
    assert b"do-not-record" not in manifest_bytes
    assert b"secret" not in manifest_bytes
    document = json.loads(manifest_bytes)
    assert document["manifest_digest"] == first.manifest.digest()

    second = backend.initialize(VersionControlRequest(cwd=repository))

    assert second.status is InitializationStatus.UNCHANGED
    assert second.manifest == first.manifest
    assert manifest_path.read_bytes() == manifest_bytes


def test_initializer_refreshes_only_the_local_branch_seed_after_head_changes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/repository.git")
    backend = GitVersionControlBackend()
    first = backend.initialize(VersionControlRequest(cwd=repository))

    (repository / "change.txt").write_text("new head\n", encoding="utf-8")
    _git(repository, "add", "change.txt")
    _git(repository, "commit", "-m", "change")
    new_head = _git(repository, "rev-parse", "HEAD")
    second = backend.initialize(VersionControlRequest(cwd=repository))

    assert first.manifest.repository.head_sha != new_head
    assert second.status is InitializationStatus.INITIALIZED
    assert second.manifest.repository.head_sha == new_head
    assert (
        _git(
            repository,
            "--git-dir",
            second.manifest.coordination.repository_path,
            "rev-parse",
            "refs/heads/main",
        )
        == new_head
    )


def test_initializer_resolves_nested_paths_and_metacharacters_without_a_shell(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, name="repository ; touch escaped")
    nested = repository / "nested directory"
    nested.mkdir()
    _git(repository, "remote", "add", "origin", "https://example.invalid/repository.git")

    result = GitVersionControlBackend().initialize(VersionControlRequest(cwd=nested))

    assert result.manifest.repository.root == str(repository.resolve())
    assert not (tmp_path / "escaped").exists()


def test_initializer_fails_closed_on_ambiguous_or_conflicting_remotes(
    tmp_path: Path,
) -> None:
    ambiguous = _repository(tmp_path, name="ambiguous")
    _git(ambiguous, "remote", "add", "origin", "https://example.invalid/origin.git")
    _git(ambiguous, "remote", "add", "backup", "https://example.invalid/backup.git")
    backend = GitVersionControlBackend()

    with pytest.raises(
        VersionControlInitializationError,
        match="upstream remote is ambiguous; pass --upstream-remote",
    ):
        backend.initialize(VersionControlRequest(cwd=ambiguous))
    assert not (ambiguous / ".git" / "af").exists()

    conflict = _repository(tmp_path, name="conflict")
    _git(conflict, "remote", "add", "origin", "https://example.invalid/origin.git")
    _git(conflict, "remote", "add", "af-local", str(tmp_path / "someone-elses.git"))

    with pytest.raises(
        VersionControlInitializationError,
        match="coordination remote already points elsewhere",
    ):
        backend.initialize(VersionControlRequest(cwd=conflict))
    assert _git(conflict, "remote", "get-url", "af-local") == str(tmp_path / "someone-elses.git")


def test_initializer_uses_an_explicit_upstream_and_rejects_detached_head(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    _git(repository, "remote", "add", "backup", "https://example.invalid/backup.git")
    backend = GitVersionControlBackend()

    initialized = backend.initialize(
        VersionControlRequest(cwd=repository, upstream_remote="backup")
    )

    assert initialized.manifest.upstream_remote.name == "backup"

    detached = _repository(tmp_path, name="detached")
    _git(detached, "remote", "add", "origin", "https://example.invalid/origin.git")
    _git(detached, "checkout", "--detach")

    with pytest.raises(
        VersionControlInitializationError,
        match="Git repository is in detached HEAD state",
    ):
        backend.initialize(VersionControlRequest(cwd=detached))
    assert not (detached / ".git" / "af").exists()


def test_initializer_rejects_non_repository_and_invalid_remote_names(
    tmp_path: Path,
) -> None:
    backend = GitVersionControlBackend()

    with pytest.raises(
        VersionControlInitializationError,
        match="current directory is not a Git worktree",
    ):
        backend.initialize(VersionControlRequest(cwd=tmp_path))

    with pytest.raises(ValueError, match="coordination remote name is invalid"):
        VersionControlRequest(cwd=tmp_path, coordination_remote="../escape")


def test_initializer_redacts_unknown_remote_helpers(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "ext::helper --credential do-not-record",
    )

    result = GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))
    recorded = result.manifest.file_bytes()

    assert result.manifest.upstream_remote.fetch_url == "ext:<redacted>"
    assert result.manifest.upstream_remote.push_url == "ext:<redacted>"
    assert b"do-not-record" not in recorded


def test_initializer_prefers_the_tracked_remote_and_redacts_scp_urls(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "git@github.example:owner/repository.git?token=do-not-record",
    )
    _git(repository, "remote", "add", "backup", "https://example.invalid/backup.git")
    head = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-ref", "refs/remotes/origin/main", head)
    _git(repository, "config", "branch.main.remote", "origin")
    _git(repository, "config", "branch.main.merge", "refs/heads/main")

    result = GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))

    assert result.manifest.upstream_remote.name == "origin"
    assert result.manifest.upstream_remote.fetch_url == ("github.example:owner/repository.git")
    assert b"do-not-record" not in result.manifest.file_bytes()


def test_initializer_rejects_missing_commit_or_upstream(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "--initial-branch=main")
    backend = GitVersionControlBackend()

    with pytest.raises(
        VersionControlInitializationError,
        match="Git repository has no commit",
    ):
        backend.initialize(VersionControlRequest(cwd=empty))

    repository = _repository(tmp_path, name="no-upstream")
    with pytest.raises(
        VersionControlInitializationError,
        match="upstream remote is not configured",
    ):
        backend.initialize(VersionControlRequest(cwd=repository))


def test_initializer_rejects_corrupt_symlinked_or_conflicting_managed_state(
    tmp_path: Path,
) -> None:
    backend = GitVersionControlBackend()

    corrupt = _repository(tmp_path, name="corrupt")
    _git(corrupt, "remote", "add", "origin", "https://example.invalid/origin.git")
    first = backend.initialize(VersionControlRequest(cwd=corrupt))
    Path(first.manifest_path).write_text("not-json", encoding="utf-8")
    with pytest.raises(
        VersionControlInitializationError,
        match="managed version-control state is invalid",
    ):
        backend.initialize(VersionControlRequest(cwd=corrupt))

    symlinked = _repository(tmp_path, name="symlinked")
    _git(symlinked, "remote", "add", "origin", "https://example.invalid/origin.git")
    outside = tmp_path / "outside"
    outside.mkdir()
    (symlinked / ".git" / "af").symlink_to(outside, target_is_directory=True)
    with pytest.raises(
        VersionControlInitializationError,
        match="managed version-control state is invalid",
    ):
        backend.initialize(VersionControlRequest(cwd=symlinked))

    conflict = _repository(tmp_path, name="manifest-conflict")
    _git(conflict, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend.initialize(VersionControlRequest(cwd=conflict))
    with pytest.raises(
        VersionControlInitializationError,
        match="managed version-control state conflicts with this repository",
    ):
        backend.initialize(VersionControlRequest(cwd=conflict, coordination_remote="other-local"))


def test_initializer_rejects_invalid_local_repository_configuration(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    result = backend.initialize(VersionControlRequest(cwd=repository))
    coordination = Path(result.manifest.coordination.repository_path)
    _git(
        repository,
        "--git-dir",
        str(coordination),
        "config",
        "--local",
        "core.hooksPath",
        str(tmp_path / "unexpected-hooks"),
    )

    with pytest.raises(
        VersionControlInitializationError,
        match="local coordination repository configuration is invalid",
    ):
        backend.initialize(VersionControlRequest(cwd=repository))


def test_initializer_does_not_overwrite_a_divergent_local_branch(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    first = backend.initialize(VersionControlRequest(cwd=repository))
    initial_head = first.manifest.repository.head_sha

    (repository / "forward.txt").write_text("forward\n", encoding="utf-8")
    _git(repository, "add", "forward.txt")
    _git(repository, "commit", "-m", "forward")
    forwarded = backend.initialize(VersionControlRequest(cwd=repository))
    local_head = forwarded.manifest.repository.head_sha

    _git(repository, "reset", "--hard", initial_head)
    (repository / "divergent.txt").write_text("divergent\n", encoding="utf-8")
    _git(repository, "add", "divergent.txt")
    _git(repository, "commit", "-m", "divergent")

    with pytest.raises(
        VersionControlInitializationError,
        match="Git version-control initialization failed",
    ):
        backend.initialize(VersionControlRequest(cwd=repository))
    assert (
        _git(
            repository,
            "--git-dir",
            forwarded.manifest.coordination.repository_path,
            "rev-parse",
            "refs/heads/main",
        )
        == local_head
    )


def test_initializer_rejects_unavailable_git_and_cwd(tmp_path: Path) -> None:
    with pytest.raises(
        VersionControlInitializationError,
        match="Git executable is unavailable",
    ):
        GitVersionControlBackend(git_executable=tmp_path)

    backend = GitVersionControlBackend()
    unavailable = tmp_path / "missing"
    with pytest.raises(
        VersionControlInitializationError,
        match="current directory is unavailable",
    ):
        backend.initialize(VersionControlRequest(cwd=unavailable))

    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("fixture", encoding="utf-8")
    with pytest.raises(
        VersionControlInitializationError,
        match="current directory is unavailable",
    ):
        backend.initialize(VersionControlRequest(cwd=not_a_directory))


def test_initializer_rejects_missing_git_on_path(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(git_version_control_module.shutil, "which", lambda _: None)

    with pytest.raises(
        VersionControlInitializationError,
        match="Git executable is unavailable",
    ):
        GitVersionControlBackend()


def test_initializer_rejects_explicit_or_recorded_upstream_drift(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    _git(repository, "remote", "add", "backup", "https://example.invalid/backup.git")
    backend = GitVersionControlBackend()
    backend.initialize(VersionControlRequest(cwd=repository, upstream_remote="origin"))

    with pytest.raises(
        VersionControlInitializationError,
        match="managed upstream remote conflicts with this request",
    ):
        backend.initialize(VersionControlRequest(cwd=repository, upstream_remote="backup"))

    _git(repository, "remote", "remove", "origin")
    with pytest.raises(
        VersionControlInitializationError,
        match="managed upstream remote does not exist",
    ):
        backend.initialize(VersionControlRequest(cwd=repository))


def test_initializer_rejects_missing_explicit_or_tracked_upstream(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, name="explicit")
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    with pytest.raises(
        VersionControlInitializationError,
        match="upstream remote does not exist",
    ):
        backend.initialize(VersionControlRequest(cwd=repository, upstream_remote="missing"))

    tracked = _repository(tmp_path, name="tracked")
    _git(tracked, "remote", "add", "origin", "https://example.invalid/origin.git")
    _git(tracked, "config", "branch.main.remote", "missing")
    _git(tracked, "config", "branch.main.merge", "refs/heads/main")
    with pytest.raises(
        VersionControlInitializationError,
        match="tracked upstream remote does not exist",
    ):
        backend.initialize(VersionControlRequest(cwd=tracked))


@pytest.mark.parametrize("manifest_shape", ["symlink", "oversized"])
def test_initializer_rejects_invalid_existing_manifest_shapes(
    tmp_path: Path,
    manifest_shape: str,
) -> None:
    repository = _repository(tmp_path, name=manifest_shape)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    result = backend.initialize(VersionControlRequest(cwd=repository))
    manifest_path = Path(result.manifest_path)
    manifest_path.unlink()
    if manifest_shape == "symlink":
        target = tmp_path / "outside-manifest"
        target.write_text("{}", encoding="utf-8")
        manifest_path.symlink_to(target)
    else:
        manifest_path.write_bytes(b"x" * (64 * 1024 + 1))

    with pytest.raises(
        VersionControlInitializationError,
        match="managed version-control state is invalid",
    ):
        backend.initialize(VersionControlRequest(cwd=repository))


@pytest.mark.parametrize("coordination_shape", ["symlink", "file", "non-bare"])
def test_initializer_rejects_invalid_existing_coordination_repository(
    tmp_path: Path,
    coordination_shape: str,
) -> None:
    repository = _repository(tmp_path, name=coordination_shape)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    coordination = repository / ".git" / "af" / "remotes" / "coordination.git"
    coordination.parent.mkdir(parents=True)
    if coordination_shape == "symlink":
        outside = tmp_path / "outside-coordination"
        outside.mkdir()
        coordination.symlink_to(outside, target_is_directory=True)
    elif coordination_shape == "file":
        coordination.write_text("not a repository", encoding="utf-8")
    else:
        coordination.mkdir()

    with pytest.raises(
        VersionControlInitializationError,
        match="local coordination repository",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))
    assert not (repository / ".git" / "af" / "worktrees").exists()


def test_initializer_preserves_an_already_correct_unborn_bare_head(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path, branch="master")
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")

    result = GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))

    coordination = result.manifest.coordination.repository_path
    assert (
        _git(
            repository,
            "--git-dir",
            coordination,
            "symbolic-ref",
            "HEAD",
        )
        == "refs/heads/master"
    )


@pytest.mark.parametrize(
    "remote_url, expected",
    [
        ("/tmp/repository.git?credential=hidden", "/tmp/repository.git"),
        ("opaque credential", "<redacted-remote-url>"),
    ],
)
def test_initializer_redacts_local_or_opaque_remote_forms(
    tmp_path: Path,
    remote_url: str,
    expected: str,
) -> None:
    repository = _repository(tmp_path, name=expected.replace("/", "_"))
    _git(repository, "remote", "add", "origin", remote_url)

    result = GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))

    assert result.manifest.upstream_remote.fetch_url == expected
    assert "credential" not in result.manifest.file_bytes().decode()


def test_initializer_preserves_the_source_object_format(tmp_path: Path) -> None:
    repository = _repository(tmp_path, object_format="sha256")
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")

    result = GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))

    assert result.manifest.repository.object_format is GitObjectFormat.SHA256
    assert len(result.manifest.repository.head_sha) == 64
    assert (
        _git(
            repository,
            "--git-dir",
            result.manifest.coordination.repository_path,
            "rev-parse",
            "--show-object-format",
        )
        == "sha256"
    )


def test_initializer_rejects_coordination_pushurl_drift(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    result = backend.initialize(VersionControlRequest(cwd=repository))
    _git(
        repository,
        "config",
        "remote.af-local.pushurl",
        "https://example.invalid/not-local.git",
    )

    with pytest.raises(
        VersionControlInitializationError,
        match="coordination remote already points elsewhere",
    ):
        backend.initialize(VersionControlRequest(cwd=repository))
    assert _git(repository, "remote", "get-url", "af-local") == (
        result.manifest.coordination.repository_path
    )


@pytest.mark.parametrize("component", ["objects", "refs"])
def test_initializer_rejects_internal_coordination_symlinks(
    tmp_path: Path,
    component: str,
) -> None:
    repository = _repository(tmp_path, name=f"internal-{component}")
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    result = backend.initialize(VersionControlRequest(cwd=repository))
    coordination = Path(result.manifest.coordination.repository_path)
    managed_component = coordination / component
    outside = tmp_path / f"outside-{component}"
    managed_component.rename(outside)
    managed_component.symlink_to(outside, target_is_directory=True)

    with pytest.raises(
        VersionControlInitializationError,
        match="local coordination repository storage is invalid",
    ):
        backend.initialize(VersionControlRequest(cwd=repository))


def test_initializer_rejects_local_config_includes_before_creating_state(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    include = tmp_path / "included-config"
    include.write_text(
        '[url "ssh://example.invalid/"]\n\tinsteadOf = /tmp/\n',
        encoding="utf-8",
    )
    _git(repository, "config", "include.path", str(include))

    with pytest.raises(
        VersionControlInitializationError,
        match="repository Git configuration is unsupported",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))
    assert not (repository / ".git" / "af").exists()


def test_concurrent_initializers_serialize_to_one_valid_result(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")

    def initialize() -> InitializationStatus:
        return GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository)).status

    with ThreadPoolExecutor(max_workers=8) as workers:
        statuses = tuple(workers.map(lambda _: initialize(), range(16)))

    assert statuses.count(InitializationStatus.INITIALIZED) == 1
    assert statuses.count(InitializationStatus.UNCHANGED) == 15
    final = GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))
    assert final.status is InitializationStatus.UNCHANGED


def test_initializer_rejects_missing_owned_remote_or_repository(tmp_path: Path) -> None:
    backend = GitVersionControlBackend()
    missing_remote = _repository(tmp_path, name="missing-remote")
    _git(
        missing_remote,
        "remote",
        "add",
        "origin",
        "https://example.invalid/origin.git",
    )
    backend.initialize(VersionControlRequest(cwd=missing_remote))
    _git(missing_remote, "remote", "remove", "af-local")
    with pytest.raises(
        VersionControlInitializationError,
        match="managed coordination remote does not exist",
    ):
        backend.initialize(VersionControlRequest(cwd=missing_remote))

    missing_repository = _repository(tmp_path, name="missing-repository")
    _git(
        missing_repository,
        "remote",
        "add",
        "origin",
        "https://example.invalid/origin.git",
    )
    result = backend.initialize(VersionControlRequest(cwd=missing_repository))
    coordination = Path(result.manifest.coordination.repository_path)
    displaced = tmp_path / "displaced-coordination"
    coordination.rename(displaced)
    with pytest.raises(
        VersionControlInitializationError,
        match="managed local coordination repository does not exist",
    ):
        backend.initialize(VersionControlRequest(cwd=missing_repository))


@pytest.mark.parametrize("tamper", ["alternate", "hook"])
def test_initializer_rejects_dangerous_bare_repository_extensions(
    tmp_path: Path,
    tamper: str,
) -> None:
    repository = _repository(tmp_path, name=f"bare-{tamper}")
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    result = backend.initialize(VersionControlRequest(cwd=repository))
    coordination = Path(result.manifest.coordination.repository_path)
    if tamper == "alternate":
        (coordination / "objects" / "info" / "alternates").write_text(
            str(tmp_path),
            encoding="utf-8",
        )
        message = "local coordination repository storage is invalid"
    else:
        hook = coordination / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o700)
        message = "local coordination repository hooks are invalid"

    with pytest.raises(VersionControlInitializationError, match=message):
        backend.initialize(VersionControlRequest(cwd=repository))


def test_initializer_rejects_invalid_lock_or_reserved_worktree_path(
    tmp_path: Path,
) -> None:
    backend = GitVersionControlBackend()
    invalid_lock = _repository(tmp_path, name="invalid-lock")
    _git(
        invalid_lock,
        "remote",
        "add",
        "origin",
        "https://example.invalid/origin.git",
    )
    (invalid_lock / ".git" / "af-version-control.lock").mkdir()
    with pytest.raises(
        VersionControlInitializationError,
        match="version-control initialization lock is invalid",
    ):
        backend.initialize(VersionControlRequest(cwd=invalid_lock))

    invalid_worktrees = _repository(tmp_path, name="invalid-worktrees")
    _git(
        invalid_worktrees,
        "remote",
        "add",
        "origin",
        "https://example.invalid/origin.git",
    )
    af_dir = invalid_worktrees / ".git" / "af"
    af_dir.mkdir()
    (af_dir / "worktrees").write_text("not a directory", encoding="utf-8")
    with pytest.raises(
        VersionControlInitializationError,
        match="managed version-control state is invalid",
    ):
        backend.initialize(VersionControlRequest(cwd=invalid_worktrees))
    assert not (af_dir / "remotes").exists()


def test_initializer_ignores_git_environment_redirection(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path, name="intended")
    other = _repository(tmp_path, name="other")
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(tmp_path / "unexpected-hooks"))

    result = GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))

    assert result.manifest.repository.root == str(repository)
    assert not (other / ".git" / "af").exists()


@pytest.mark.parametrize("url_kind", ["missing", "empty", "multiple-fetch", "multiple-push"])
def test_initializer_rejects_ambiguous_upstream_url_configuration(
    tmp_path: Path,
    url_kind: str,
) -> None:
    repository = _repository(tmp_path, name=url_kind)
    if url_kind == "missing":
        _git(repository, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    else:
        _git(repository, "remote", "add", "origin", "https://example.invalid/one.git")
        if url_kind == "empty":
            _git(repository, "config", "remote.origin.url", "")
        elif url_kind == "multiple-fetch":
            _git(
                repository,
                "config",
                "--add",
                "remote.origin.url",
                "https://example.invalid/two.git",
            )
        else:
            _git(
                repository,
                "config",
                "--add",
                "remote.origin.pushurl",
                "https://example.invalid/one.git",
            )
            _git(
                repository,
                "config",
                "--add",
                "remote.origin.pushurl",
                "https://example.invalid/two.git",
            )

    with pytest.raises(
        VersionControlInitializationError,
        match="(?:upstream remote|repository Git) configuration is invalid",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))
    assert not (repository / ".git" / "af").exists()


def test_initializer_rejects_multiple_tracked_remotes_and_unowned_exact_remote(
    tmp_path: Path,
) -> None:
    tracked = _repository(tmp_path, name="multiple-tracked")
    _git(tracked, "remote", "add", "origin", "https://example.invalid/origin.git")
    _git(tracked, "remote", "add", "backup", "https://example.invalid/backup.git")
    _git(tracked, "config", "--add", "branch.main.remote", "origin")
    _git(tracked, "config", "--add", "branch.main.remote", "backup")

    with pytest.raises(
        VersionControlInitializationError,
        match="tracked upstream remote configuration is invalid",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=tracked))

    unowned = _repository(tmp_path, name="unowned-remote")
    _git(unowned, "remote", "add", "origin", "https://example.invalid/origin.git")
    coordination = unowned / ".git" / "af" / "remotes" / "coordination.git"
    _git(unowned, "remote", "add", "af-local", str(coordination))

    with pytest.raises(
        VersionControlInitializationError,
        match="coordination remote is not owned by this AF initialization",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=unowned))


@pytest.mark.parametrize(
    ("tamper", "message"),
    [
        ("file", "local coordination repository is invalid"),
        ("non-bare", "local coordination repository is invalid"),
        ("object-format", "local coordination repository object format is invalid"),
        ("hooks-file", "local coordination repository storage is invalid"),
    ],
)
def test_initializer_rejects_owned_coordination_repository_shape_drift(
    tmp_path: Path,
    tamper: str,
    message: str,
) -> None:
    repository = _repository(tmp_path, name=f"owned-{tamper}")
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    result = backend.initialize(VersionControlRequest(cwd=repository))
    coordination = Path(result.manifest.coordination.repository_path)

    if tamper == "hooks-file":
        hooks = coordination / "hooks"
        hooks.rename(tmp_path / f"{tamper}-hooks")
        hooks.write_text("not a directory", encoding="utf-8")
    else:
        coordination.rename(tmp_path / f"{tamper}-coordination")
        if tamper == "file":
            coordination.write_text("not a repository", encoding="utf-8")
        elif tamper == "non-bare":
            coordination.mkdir()
        else:
            _git(
                repository,
                "init",
                "--bare",
                "--object-format=sha256",
                str(coordination),
            )

    with pytest.raises(VersionControlInitializationError, match=message):
        backend.initialize(VersionControlRequest(cwd=repository))


def test_initializer_rejects_nested_symlinks_and_excessive_managed_state(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    symlinked = _repository(tmp_path, name="nested-symlink")
    _git(symlinked, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend = GitVersionControlBackend()
    result = backend.initialize(VersionControlRequest(cwd=symlinked))
    coordination = Path(result.manifest.coordination.repository_path)
    (coordination / "objects" / "escape").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(
        VersionControlInitializationError,
        match="local coordination repository storage is invalid",
    ):
        backend.initialize(VersionControlRequest(cwd=symlinked))

    excessive = _repository(tmp_path, name="excessive-tree")
    _git(excessive, "remote", "add", "origin", "https://example.invalid/origin.git")
    backend.initialize(VersionControlRequest(cwd=excessive))
    monkeypatch.setattr(git_version_control_module, "_MANAGED_TREE_MAX_ENTRIES", 1)

    with pytest.raises(
        VersionControlInitializationError,
        match="local coordination repository storage is too large",
    ):
        backend.initialize(VersionControlRequest(cwd=excessive))


def test_initializer_rejects_invalid_managed_parent_and_lock_creation_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    invalid_parent = _repository(tmp_path, name="invalid-parent")
    _git(invalid_parent, "remote", "add", "origin", "https://example.invalid/origin.git")
    af_dir = invalid_parent / ".git" / "af"
    af_dir.mkdir()
    (af_dir / "remotes").write_text("not a directory", encoding="utf-8")

    with pytest.raises(
        VersionControlInitializationError,
        match="managed version-control state is invalid",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=invalid_parent))

    lock_failure = _repository(tmp_path, name="lock-failure")
    _git(lock_failure, "remote", "add", "origin", "https://example.invalid/origin.git")
    real_open = os.open

    def reject_lock(path, flags, mode=0o777):  # type: ignore[no-untyped-def]
        if Path(path).name == "af-version-control.lock":
            raise OSError
        return real_open(path, flags, mode)

    monkeypatch.setattr(git_version_control_module.os, "open", reject_lock)
    with pytest.raises(
        VersionControlInitializationError,
        match="version-control initialization lock is unavailable",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=lock_failure))


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"x" * (64 * 1024 + 1), "Git command output is too large"),
        (b"\xff", "Git command output is invalid"),
    ],
)
def test_initializer_bounds_and_validates_git_output(
    tmp_path: Path,
    monkeypatch,
    payload: bytes,
    message: str,
) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    backend = GitVersionControlBackend()

    def emit_output(*args, **kwargs):  # type: ignore[no-untyped-def]
        kwargs["stdout"].write(payload)
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(git_version_control_module.subprocess, "run", emit_output)
    with pytest.raises(VersionControlInitializationError, match=message):
        backend.initialize(VersionControlRequest(cwd=repository))


def test_initializer_sanitizes_subprocess_execution_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    repository = _repository(tmp_path)
    backend = GitVersionControlBackend()

    def fail_execution(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("sensitive execution detail")

    monkeypatch.setattr(git_version_control_module.subprocess, "run", fail_execution)
    with pytest.raises(
        VersionControlInitializationError,
        match="^Git version-control initialization failed$",
    ):
        backend.initialize(VersionControlRequest(cwd=repository))


def test_initializer_rejects_excessive_remote_count_before_creating_state(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    config = repository / ".git" / "config"
    remote_sections = "".join(
        f'[remote "remote-{index}"]\n\turl = /tmp/{index}.git\n' for index in range(1_001)
    )
    config.write_text(config.read_text(encoding="utf-8") + remote_sections, encoding="utf-8")

    with pytest.raises(
        VersionControlInitializationError,
        match="Git remote state is invalid",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))
    assert not (repository / ".git" / "af").exists()


def test_initializer_rejects_worktree_configuration_before_seed_or_mutation(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    coordination = repository / ".git" / "af" / "remotes" / "coordination.git"
    redirected = tmp_path / "redirected.git"
    _git(repository, "init", "--bare", str(redirected))
    _git(repository, "config", "extensions.worktreeConfig", "true")
    _git(
        repository,
        "config",
        "--worktree",
        f"url.{redirected}.insteadOf",
        str(coordination),
    )

    with pytest.raises(
        VersionControlInitializationError,
        match="repository worktree Git configuration is unsupported",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))

    assert not (repository / ".git" / "af").exists()
    assert _git(repository, "remote") == "origin"
    assert (
        _git(
            repository,
            "--git-dir",
            str(redirected),
            "for-each-ref",
            "--format=%(refname)",
            "refs/heads",
        )
        == ""
    )


def test_initializer_rejects_dangling_manifest_symlink_before_mutation(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _git(repository, "remote", "add", "origin", "https://example.invalid/origin.git")
    af_dir = repository / ".git" / "af"
    af_dir.mkdir()
    manifest_path = af_dir / "version-control.json"
    manifest_path.symlink_to(tmp_path / "missing-manifest")

    with pytest.raises(
        VersionControlInitializationError,
        match="managed version-control state is invalid",
    ):
        GitVersionControlBackend().initialize(VersionControlRequest(cwd=repository))

    assert manifest_path.is_symlink()
    assert not (af_dir / "remotes").exists()
    assert not (af_dir / "worktrees").exists()
    assert _git(repository, "remote") == "origin"
