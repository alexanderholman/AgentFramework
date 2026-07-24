from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from af.application.version_control import VersionControlController
from af.framework.version_control import (
    CoordinationObservation,
    GitObjectFormat,
    InitializationStatus,
    RemoteObservation,
    RepositoryObservation,
    VersionControlInitialization,
    VersionControlInitializationError,
    VersionControlManifest,
)
from af.interfaces.cli import create_cli

runner = CliRunner()


class FixtureVersionControlBackend:
    def __init__(self, result: VersionControlInitialization) -> None:
        self.result = result
        self.requests = []

    def initialize(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return self.result


class FailingVersionControlBackend:
    def initialize(self, request):  # type: ignore[no-untyped-def]
        raise VersionControlInitializationError("version-control initialization refused")


def _result(tmp_path: Path) -> VersionControlInitialization:
    manifest = VersionControlManifest(
        repository=RepositoryObservation(
            root=str(tmp_path),
            git_common_dir=str(tmp_path / ".git"),
            branch="main",
            head_sha="a" * 40,
            object_format=GitObjectFormat.SHA1,
        ),
        upstream_remote=RemoteObservation(
            name="origin",
            fetch_url="https://example.invalid/repository.git",
            push_url="https://example.invalid/repository.git",
        ),
        coordination=CoordinationObservation(
            remote_name="af-local",
            repository_path=str(tmp_path / ".git" / "af" / "remotes" / "coordination.git"),
            worktree_root=str(tmp_path / ".git" / "af" / "worktrees"),
        ),
    )
    return VersionControlInitialization(
        status=InitializationStatus.INITIALIZED,
        manifest=manifest,
        manifest_path=str(tmp_path / ".git" / "af" / "version-control.json"),
    )


def test_cli_exposes_literal_init_version_control_command(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    backend = FixtureVersionControlBackend(_result(tmp_path))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        create_cli(version_control=VersionControlController(backend)),
        [
            "init",
            "version",
            "control",
            "--upstream-remote",
            "origin",
            "--coordination-remote",
            "af-local",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == backend.result.to_dict()
    assert len(backend.requests) == 1
    assert backend.requests[0].cwd == tmp_path
    assert backend.requests[0].upstream_remote == "origin"
    assert backend.requests[0].coordination_remote == "af-local"


def test_cli_reports_sanitized_version_control_failure(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        create_cli(version_control=VersionControlController(FailingVersionControlBackend())),
        ["init", "version", "control"],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr == "version-control initialization refused\n"
