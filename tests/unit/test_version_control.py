from __future__ import annotations

import json
from pathlib import Path

import pytest

from af.application.version_control import VersionControlController
from af.bootstrap_version_control import create_version_control_controller
from af.framework.version_control import (
    MANIFEST_MAX_BYTES,
    CoordinationObservation,
    GitObjectFormat,
    InitializationStatus,
    RemoteObservation,
    RepositoryObservation,
    VersionControlInitialization,
    VersionControlManifest,
    VersionControlRequest,
)


def _manifest(tmp_path: Path) -> VersionControlManifest:
    return VersionControlManifest(
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


def _bytes(document: object) -> bytes:
    return (
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def test_manifest_round_trips_canonical_bytes_and_result(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    restored = VersionControlManifest.from_file_bytes(manifest.file_bytes())
    result = VersionControlInitialization(
        status=InitializationStatus.INITIALIZED,
        manifest=restored,
        manifest_path=str(tmp_path / ".git" / "af" / "version-control.json"),
    )

    assert restored == manifest
    assert len(manifest.digest()) == 64
    assert result.to_dict()["manifest"] == {
        "path": str(tmp_path / ".git" / "af" / "version-control.json"),
        "digest": manifest.digest(),
    }


def test_lightweight_composition_root_builds_the_controller() -> None:
    controller = create_version_control_controller()

    assert isinstance(controller, VersionControlController)


@pytest.mark.parametrize(
    "factory, message",
    [
        (
            lambda path: VersionControlRequest(cwd="not-a-path"),  # type: ignore[arg-type]
            "cwd must be a Path",
        ),
        (
            lambda path: VersionControlRequest(cwd=path, upstream_remote="bad/name"),
            "upstream remote name is invalid",
        ),
        (
            lambda path: RepositoryObservation(
                root="relative",
                git_common_dir=str(path / ".git"),
                branch="main",
                head_sha="a" * 40,
                object_format=GitObjectFormat.SHA1,
            ),
            "repository root must be absolute",
        ),
        (
            lambda path: RepositoryObservation(
                root=str(path),
                git_common_dir=str(path / ".git"),
                branch="main",
                head_sha="not-a-sha",
                object_format=GitObjectFormat.SHA1,
            ),
            "HEAD SHA is invalid",
        ),
        (
            lambda path: RepositoryObservation(
                root=str(path),
                git_common_dir=str(path / ".git"),
                branch="main",
                head_sha="a" * 40,
                object_format="sha512",  # type: ignore[arg-type]
            ),
            "Git object format is invalid",
        ),
        (
            lambda path: RemoteObservation(
                name="origin",
                fetch_url="https://example.invalid/\nsecret",
                push_url="https://example.invalid/repository.git",
            ),
            "fetch URL is invalid",
        ),
        (
            lambda path: VersionControlInitialization(
                status="initialized",  # type: ignore[arg-type]
                manifest=_manifest(path),
                manifest_path=str(path / "manifest.json"),
            ),
            "status must be an InitializationStatus",
        ),
    ],
)
def test_contracts_reject_malformed_values(
    tmp_path: Path,
    factory,  # type: ignore[no-untyped-def]
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        factory(tmp_path)


def test_manifest_parser_rejects_malformed_noncanonical_or_unbound_content(
    tmp_path: Path,
) -> None:
    manifest = _manifest(tmp_path)
    valid = json.loads(manifest.file_bytes())
    cases: list[bytes] = [
        b"",
        b"x" * (MANIFEST_MAX_BYTES + 1),
        b"{",
        _bytes([]),
        _bytes({**valid, "extra": True}),
        _bytes({**valid, "schema_version": "future"}),
        _bytes({**valid, "repository": []}),
        _bytes({**valid, "repository": {"root": str(tmp_path)}}),
        _bytes({**valid, "upstream_remote": {"name": "origin"}}),
        _bytes({**valid, "coordination": {"remote_name": "af-local"}}),
        _bytes({**valid, "manifest_digest": "0" * 64}),
        json.dumps(valid, indent=2).encode(),
        b"\xff",
    ]

    for content in cases:
        with pytest.raises(ValueError, match="version-control manifest is invalid"):
            VersionControlManifest.from_file_bytes(content)
