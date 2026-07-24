"""Immutable contracts for AF's repository-local version-control bootstrap."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from af.framework.errors import AFError

VERSION_CONTROL_SCHEMA = "af.version-control/v1"
MANIFEST_MAX_BYTES = 64 * 1024
_PATH_MAX_LENGTH = 8_192
_URL_MAX_LENGTH = 8_192
_BRANCH_MAX_LENGTH = 1_024
_REMOTE_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HEAD_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")


class VersionControlInitializationError(AFError):
    """A sanitized, fail-closed version-control bootstrap error."""


class InitializationStatus(StrEnum):
    INITIALIZED = "initialized"
    UNCHANGED = "unchanged"


class GitObjectFormat(StrEnum):
    SHA1 = "sha1"
    SHA256 = "sha256"


def _bounded_line(value: object, *, label: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or _CONTROL_PATTERN.search(value)
    ):
        raise ValueError(f"{label} is invalid")
    return value


def _remote_name(value: object, *, label: str) -> str:
    candidate = _bounded_line(value, label=label, maximum=64)
    if _REMOTE_NAME_PATTERN.fullmatch(candidate) is None:
        raise ValueError(f"{label} is invalid")
    return candidate


def _absolute_path(value: object, *, label: str) -> str:
    candidate = _bounded_line(value, label=label, maximum=_PATH_MAX_LENGTH)
    if not Path(candidate).is_absolute():
        raise ValueError(f"{label} must be absolute")
    return candidate


@dataclass(frozen=True, slots=True)
class VersionControlRequest:
    cwd: Path
    upstream_remote: str | None = None
    coordination_remote: str = "af-local"

    def __post_init__(self) -> None:
        if not isinstance(self.cwd, Path):
            raise TypeError("cwd must be a Path")
        if self.upstream_remote is not None:
            object.__setattr__(
                self,
                "upstream_remote",
                _remote_name(self.upstream_remote, label="upstream remote name"),
            )
        object.__setattr__(
            self,
            "coordination_remote",
            _remote_name(self.coordination_remote, label="coordination remote name"),
        )


@dataclass(frozen=True, slots=True)
class RepositoryObservation:
    root: str
    git_common_dir: str
    branch: str
    head_sha: str
    object_format: GitObjectFormat

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", _absolute_path(self.root, label="repository root"))
        object.__setattr__(
            self,
            "git_common_dir",
            _absolute_path(self.git_common_dir, label="Git common directory"),
        )
        object.__setattr__(
            self,
            "branch",
            _bounded_line(self.branch, label="branch", maximum=_BRANCH_MAX_LENGTH),
        )
        try:
            object_format = GitObjectFormat(self.object_format)
        except (TypeError, ValueError):
            raise ValueError("Git object format is invalid") from None
        object.__setattr__(self, "object_format", object_format)
        head_sha = _bounded_line(self.head_sha, label="HEAD SHA", maximum=64)
        if _HEAD_PATTERN.fullmatch(head_sha) is None or len(head_sha) != (
            40 if object_format is GitObjectFormat.SHA1 else 64
        ):
            raise ValueError("HEAD SHA is invalid")
        object.__setattr__(self, "head_sha", head_sha)

    def to_dict(self) -> dict[str, str]:
        return {
            "root": self.root,
            "git_common_dir": self.git_common_dir,
            "branch": self.branch,
            "head_sha": self.head_sha,
            "object_format": self.object_format.value,
        }


@dataclass(frozen=True, slots=True)
class RemoteObservation:
    name: str
    fetch_url: str
    push_url: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _remote_name(self.name, label="remote name"))
        object.__setattr__(
            self,
            "fetch_url",
            _bounded_line(self.fetch_url, label="fetch URL", maximum=_URL_MAX_LENGTH),
        )
        object.__setattr__(
            self,
            "push_url",
            _bounded_line(self.push_url, label="push URL", maximum=_URL_MAX_LENGTH),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "fetch_url": self.fetch_url,
            "push_url": self.push_url,
        }


@dataclass(frozen=True, slots=True)
class CoordinationObservation:
    remote_name: str
    repository_path: str
    worktree_root: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "remote_name",
            _remote_name(self.remote_name, label="coordination remote name"),
        )
        object.__setattr__(
            self,
            "repository_path",
            _absolute_path(
                self.repository_path,
                label="coordination repository path",
            ),
        )
        object.__setattr__(
            self,
            "worktree_root",
            _absolute_path(self.worktree_root, label="worktree root"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "remote_name": self.remote_name,
            "repository_path": self.repository_path,
            "worktree_root": self.worktree_root,
        }


@dataclass(frozen=True, slots=True)
class VersionControlManifest:
    repository: RepositoryObservation
    upstream_remote: RemoteObservation
    coordination: CoordinationObservation
    schema_version: str = field(default=VERSION_CONTROL_SCHEMA, init=False)

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "repository": self.repository.to_dict(),
            "upstream_remote": self.upstream_remote.to_dict(),
            "coordination": self.coordination.to_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.payload(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    def file_bytes(self) -> bytes:
        document = self.payload()
        document["manifest_digest"] = self.digest()
        return (
            json.dumps(
                document,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @classmethod
    def from_file_bytes(cls, content: bytes) -> VersionControlManifest:
        if not content or len(content) > MANIFEST_MAX_BYTES:
            raise ValueError("version-control manifest is invalid")
        try:
            document = json.loads(content)
            if type(document) is not dict or set(document) != {
                "schema_version",
                "manifest_digest",
                "repository",
                "upstream_remote",
                "coordination",
            }:
                raise ValueError
            if document["schema_version"] != VERSION_CONTROL_SCHEMA:
                raise ValueError
            repository = document["repository"]
            upstream = document["upstream_remote"]
            coordination = document["coordination"]
            if not all(type(value) is dict for value in (repository, upstream, coordination)):
                raise ValueError
            if set(repository) != {
                "root",
                "git_common_dir",
                "branch",
                "head_sha",
                "object_format",
            }:
                raise ValueError
            if set(upstream) != {"name", "fetch_url", "push_url"}:
                raise ValueError
            if set(coordination) != {"remote_name", "repository_path", "worktree_root"}:
                raise ValueError
            manifest = cls(
                repository=RepositoryObservation(**repository),
                upstream_remote=RemoteObservation(**upstream),
                coordination=CoordinationObservation(**coordination),
            )
            if document["manifest_digest"] != manifest.digest():
                raise ValueError
            if content != manifest.file_bytes():
                raise ValueError
        except (
            KeyError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ):
            raise ValueError("version-control manifest is invalid") from None
        return manifest


@dataclass(frozen=True, slots=True)
class VersionControlInitialization:
    status: InitializationStatus
    manifest: VersionControlManifest
    manifest_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.status, InitializationStatus):
            raise TypeError("status must be an InitializationStatus")
        object.__setattr__(
            self,
            "manifest_path",
            _absolute_path(self.manifest_path, label="manifest path"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            **self.manifest.payload(),
            "manifest": {
                "path": self.manifest_path,
                "digest": self.manifest.digest(),
            },
        }


class VersionControlBackend(Protocol):
    def initialize(self, request: VersionControlRequest) -> VersionControlInitialization:
        """Initialize or inspect repository-local coordination state."""

        ...
