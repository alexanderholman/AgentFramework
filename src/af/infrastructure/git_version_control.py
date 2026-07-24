"""Git-backed, repository-local AF version-control initialization."""

from __future__ import annotations

import hmac
import importlib
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from af.framework.version_control import (
    MANIFEST_MAX_BYTES,
    CoordinationObservation,
    GitObjectFormat,
    InitializationStatus,
    RemoteObservation,
    RepositoryObservation,
    VersionControlInitialization,
    VersionControlInitializationError,
    VersionControlManifest,
    VersionControlRequest,
)

_COMMAND_TIMEOUT_SECONDS = 30
_COMMAND_OUTPUT_MAX_BYTES = 64 * 1024
_MANAGED_TREE_MAX_ENTRIES = 250_000
_SCP_REMOTE_PATTERN = re.compile(r"^(?:[^/@\s]+@)?([^:/\s]+):(.+)$")
_SAFE_BARE_CONFIG_KEYS = frozenset(
    {
        "core.bare",
        "core.filemode",
        "core.ignorecase",
        "core.logallrefupdates",
        "core.precomposeunicode",
        "core.repositoryformatversion",
        "core.symlinks",
        "extensions.objectformat",
    }
)


class GitVersionControlBackend:
    """Create one private bare remote without contacting the upstream remote."""

    def __init__(self, git_executable: str | Path | None = None) -> None:
        candidate = shutil.which("git") if git_executable is None else os.fspath(git_executable)
        if candidate is None:
            raise VersionControlInitializationError("Git executable is unavailable")
        resolved = Path(candidate).resolve()
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise VersionControlInitializationError("Git executable is unavailable")
        self._git = str(resolved)

    def initialize(self, request: VersionControlRequest) -> VersionControlInitialization:
        cwd = self._resolve_cwd(request.cwd)
        if self._try_git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
            raise VersionControlInitializationError("current directory is not a Git worktree")
        root = self._resolve_git_path(
            cwd,
            self._git_output(cwd, "rev-parse", "--show-toplevel"),
        )
        common_dir = self._resolve_git_path(
            cwd,
            self._git_output(cwd, "rev-parse", "--git-common-dir"),
        )
        with self._repository_lock(common_dir):
            return self._initialize_locked(
                request,
                cwd=cwd,
                expected_root=root,
                expected_common_dir=common_dir,
            )

    def _initialize_locked(
        self,
        request: VersionControlRequest,
        *,
        cwd: Path,
        expected_root: Path,
        expected_common_dir: Path,
    ) -> VersionControlInitialization:
        if self._try_git(cwd, "rev-parse", "--is-inside-work-tree") != "true":
            raise VersionControlInitializationError("current directory is not a Git worktree")
        root = self._resolve_git_path(
            cwd,
            self._git_output(cwd, "rev-parse", "--show-toplevel"),
        )
        common_dir = self._resolve_git_path(
            cwd,
            self._git_output(cwd, "rev-parse", "--git-common-dir"),
        )
        if root != expected_root or common_dir != expected_common_dir:
            raise VersionControlInitializationError("Git repository changed during initialization")
        head_sha = self._try_git(cwd, "rev-parse", "--verify", "HEAD^{commit}")
        if head_sha is None:
            raise VersionControlInitializationError("Git repository has no commit")
        branch = self._try_git(cwd, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch is None:
            raise VersionControlInitializationError("Git repository is in detached HEAD state")
        object_format_value = self._git_output(
            cwd,
            "rev-parse",
            "--show-object-format",
        )
        try:
            object_format = GitObjectFormat(object_format_value)
        except ValueError:
            raise VersionControlInitializationError("Git object format is unsupported") from None

        af_dir = common_dir / "af"
        coordination_path = af_dir / "remotes" / "coordination.git"
        worktree_root = af_dir / "worktrees"
        manifest_path = af_dir / "version-control.json"
        self._validate_managed_path(af_dir)
        self._validate_directory_candidate(worktree_root)
        previous = self._read_manifest(manifest_path)
        self._validate_previous_manifest(
            previous,
            root=root,
            common_dir=common_dir,
            coordination_path=coordination_path,
            worktree_root=worktree_root,
            coordination_remote=request.coordination_remote,
        )

        remotes = self._remote_names(root)
        self._validate_source_configuration(root)
        self._validate_existing_coordination_remote(
            root,
            remotes=remotes,
            remote_name=request.coordination_remote,
            coordination_path=coordination_path,
            must_exist=previous is not None,
        )
        upstream_name = self._select_upstream(
            root,
            branch=branch,
            remotes=remotes,
            request=request,
            previous=previous,
        )
        upstream_urls = self._config_values(
            root,
            f"remote.{upstream_name}.url",
        )
        upstream_push_urls = self._config_values(
            root,
            f"remote.{upstream_name}.pushurl",
        )
        if len(upstream_urls) != 1 or len(upstream_push_urls) > 1:
            raise VersionControlInitializationError("upstream remote configuration is invalid")
        upstream = RemoteObservation(
            name=upstream_name,
            fetch_url=self._redact_remote_url(upstream_urls[0]),
            push_url=self._redact_remote_url(
                upstream_push_urls[0] if upstream_push_urls else upstream_urls[0]
            ),
        )

        changed = False
        for directory in (af_dir, coordination_path.parent):
            changed = self._ensure_directory(common_dir, directory) or changed
        coordination_created = self._ensure_bare_repository(
            root,
            coordination_path,
            object_format=object_format,
            owned=previous is not None,
        )
        changed = coordination_created or changed
        self._validate_bare_repository(
            root,
            coordination_path=coordination_path,
            object_format=object_format,
        )
        changed = self._ensure_directory(common_dir, worktree_root) or changed
        changed = (
            self._ensure_bare_head(
                root,
                coordination_path=coordination_path,
                branch=branch,
            )
            or changed
        )
        changed = (
            self._seed_branch(
                root,
                coordination_path=coordination_path,
                branch=branch,
                head_sha=head_sha,
            )
            or changed
        )
        self._validate_bare_repository(
            root,
            coordination_path=coordination_path,
            object_format=object_format,
        )

        if request.coordination_remote not in remotes:
            self._git_output(
                root,
                "remote",
                "add",
                request.coordination_remote,
                str(coordination_path),
                capture=False,
            )
            changed = True

        manifest = VersionControlManifest(
            repository=RepositoryObservation(
                root=str(root),
                git_common_dir=str(common_dir),
                branch=branch,
                head_sha=head_sha,
                object_format=object_format,
            ),
            upstream_remote=upstream,
            coordination=CoordinationObservation(
                remote_name=request.coordination_remote,
                repository_path=str(coordination_path),
                worktree_root=str(worktree_root),
            ),
        )
        manifest_bytes = manifest.file_bytes()
        if previous is None or manifest_bytes != previous.file_bytes():
            self._write_manifest(manifest_path, manifest_bytes)
            changed = True

        return VersionControlInitialization(
            status=(
                InitializationStatus.INITIALIZED if changed else InitializationStatus.UNCHANGED
            ),
            manifest=manifest,
            manifest_path=str(manifest_path),
        )

    @staticmethod
    def _resolve_cwd(cwd: Path) -> Path:
        try:
            resolved = cwd.resolve(strict=True)
        except OSError:
            raise VersionControlInitializationError("current directory is unavailable") from None
        if not resolved.is_dir():
            raise VersionControlInitializationError("current directory is unavailable")
        return resolved

    @staticmethod
    @contextmanager
    def _repository_lock(common_dir: Path) -> Iterator[None]:
        lock_path = common_dir / "af-version-control.lock"
        if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
            raise VersionControlInitializationError(
                "version-control initialization lock is invalid"
            )
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
        except OSError:
            raise VersionControlInitializationError(
                "version-control initialization lock is unavailable"
            ) from None
        windows_lock = os.name == "nt"
        try:
            if windows_lock:
                msvcrt = importlib.import_module("msvcrt")

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError:
            os.close(descriptor)
            raise VersionControlInitializationError(
                "version-control initialization lock failed"
            ) from None
        try:
            yield
        finally:
            try:
                if windows_lock:
                    msvcrt = importlib.import_module("msvcrt")

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(descriptor)

    @staticmethod
    def _resolve_git_path(cwd: Path, value: str) -> Path:
        candidate = Path(value)
        try:
            return (candidate if candidate.is_absolute() else cwd / candidate).resolve(strict=True)
        except OSError:
            raise VersionControlInitializationError("Git repository metadata is invalid") from None

    @staticmethod
    def _validate_managed_path(af_dir: Path) -> None:
        if af_dir.is_symlink() or (af_dir.exists() and not af_dir.is_dir()):
            raise VersionControlInitializationError("managed version-control state is invalid")

    @staticmethod
    def _validate_directory_candidate(path: Path) -> None:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise VersionControlInitializationError("managed version-control state is invalid")

    @staticmethod
    def _read_manifest(path: Path) -> VersionControlManifest | None:
        if path.is_symlink():
            raise VersionControlInitializationError("managed version-control state is invalid")
        if not path.exists():
            return None
        if not path.is_file():
            raise VersionControlInitializationError("managed version-control state is invalid")
        try:
            if path.stat().st_size > MANIFEST_MAX_BYTES:
                raise ValueError
            return VersionControlManifest.from_file_bytes(path.read_bytes())
        except (OSError, ValueError):
            raise VersionControlInitializationError(
                "managed version-control state is invalid"
            ) from None

    @staticmethod
    def _validate_previous_manifest(
        previous: VersionControlManifest | None,
        *,
        root: Path,
        common_dir: Path,
        coordination_path: Path,
        worktree_root: Path,
        coordination_remote: str,
    ) -> None:
        if previous is None:
            return
        if (
            previous.repository.root != str(root)
            or previous.repository.git_common_dir != str(common_dir)
            or previous.coordination.remote_name != coordination_remote
            or previous.coordination.repository_path != str(coordination_path)
            or previous.coordination.worktree_root != str(worktree_root)
        ):
            raise VersionControlInitializationError(
                "managed version-control state conflicts with this repository"
            )

    def _remote_names(self, root: Path) -> tuple[str, ...]:
        output = self._git_output(root, "remote")
        names = tuple(sorted(line for line in output.splitlines() if line))
        if len(names) > 1_000 or len(names) != len(set(names)):
            raise VersionControlInitializationError("Git remote state is invalid")
        return names

    def _config_values(self, root: Path, key: str) -> tuple[str, ...]:
        output = self._try_git(
            root,
            "config",
            "--local",
            "--no-includes",
            "--null",
            "--get-all",
            key,
        )
        if output is None:
            return ()
        values = output.split("\0")
        if values and values[-1] == "":
            values.pop()
        if not values or any(not value for value in values):
            raise VersionControlInitializationError("repository Git configuration is invalid")
        return tuple(values)

    def _validate_existing_coordination_remote(
        self,
        root: Path,
        *,
        remotes: tuple[str, ...],
        remote_name: str,
        coordination_path: Path,
        must_exist: bool,
    ) -> None:
        if remote_name not in remotes:
            if must_exist:
                raise VersionControlInitializationError(
                    "managed coordination remote does not exist"
                )
            return
        configured = self._config_values(root, f"remote.{remote_name}.url")
        push_urls = self._config_values(root, f"remote.{remote_name}.pushurl")
        if configured != (str(coordination_path),) or push_urls:
            raise VersionControlInitializationError("coordination remote already points elsewhere")
        if not must_exist:
            raise VersionControlInitializationError(
                "coordination remote is not owned by this AF initialization"
            )

    def _select_upstream(
        self,
        root: Path,
        *,
        branch: str,
        remotes: tuple[str, ...],
        request: VersionControlRequest,
        previous: VersionControlManifest | None,
    ) -> str:
        eligible = tuple(name for name in remotes if name != request.coordination_remote)
        if request.upstream_remote is not None:
            if previous is not None and previous.upstream_remote.name != request.upstream_remote:
                raise VersionControlInitializationError(
                    "managed upstream remote conflicts with this request"
                )
            if request.upstream_remote not in eligible:
                raise VersionControlInitializationError("upstream remote does not exist")
            return request.upstream_remote
        if previous is not None:
            if previous.upstream_remote.name not in eligible:
                raise VersionControlInitializationError("managed upstream remote does not exist")
            return previous.upstream_remote.name
        tracked_values = self._config_values(root, f"branch.{branch}.remote")
        if len(tracked_values) > 1:
            raise VersionControlInitializationError(
                "tracked upstream remote configuration is invalid"
            )
        tracked = tracked_values[0] if tracked_values else None
        if tracked and tracked not in {".", request.coordination_remote}:
            if tracked not in eligible:
                raise VersionControlInitializationError("tracked upstream remote does not exist")
            return tracked
        if len(eligible) == 1:
            return eligible[0]
        if not eligible:
            raise VersionControlInitializationError("upstream remote is not configured")
        raise VersionControlInitializationError(
            "upstream remote is ambiguous; pass --upstream-remote"
        )

    @staticmethod
    def _redact_remote_url(value: str) -> str:
        if "://" in value:
            parsed = urlsplit(value)
            netloc = parsed.netloc.rsplit("@", 1)[-1]
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        if "::" in value:
            scheme = value.split(":", 1)[0]
            return f"{scheme}:<redacted>"
        scp_match = _SCP_REMOTE_PATTERN.fullmatch(value)
        if scp_match is not None:
            path = scp_match.group(2).split("?", 1)[0].split("#", 1)[0]
            return f"{scp_match.group(1)}:{path}"
        if value.startswith(("/", "./", "../")):
            return value.split("?", 1)[0].split("#", 1)[0]
        return "<redacted-remote-url>"

    def _ensure_directory(self, common_dir: Path, path: Path) -> bool:
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise VersionControlInitializationError("managed version-control state is invalid")
        if path.exists():
            return False
        try:
            path.mkdir(mode=0o700)
        except OSError:
            raise VersionControlInitializationError(
                "managed version-control state could not be created"
            ) from None
        if not path.resolve().is_relative_to(common_dir):
            raise VersionControlInitializationError("managed version-control state is invalid")
        return True

    def _ensure_bare_repository(
        self,
        root: Path,
        coordination_path: Path,
        *,
        object_format: GitObjectFormat,
        owned: bool,
    ) -> bool:
        if coordination_path.is_symlink():
            raise VersionControlInitializationError("local coordination repository is invalid")
        if coordination_path.exists():
            if not owned:
                raise VersionControlInitializationError(
                    "unowned local coordination repository already exists"
                )
            if not coordination_path.is_dir():
                raise VersionControlInitializationError("local coordination repository is invalid")
            if (
                self._try_git(
                    root,
                    "--git-dir",
                    str(coordination_path),
                    "rev-parse",
                    "--is-bare-repository",
                )
                != "true"
            ):
                raise VersionControlInitializationError("local coordination repository is invalid")
            return False
        if owned:
            raise VersionControlInitializationError(
                "managed local coordination repository does not exist"
            )
        self._git_output(
            root,
            "init",
            "--bare",
            f"--object-format={object_format.value}",
            str(coordination_path),
            capture=False,
        )
        return True

    def _validate_source_configuration(self, root: Path) -> None:
        local_entries = self._config_entries(root)
        effective_entries = self._config_entries(root, local_only=False)
        if local_entries != effective_entries:
            raise VersionControlInitializationError(
                "repository worktree Git configuration is unsupported"
            )
        if any(key.startswith(("include.", "includeif.", "url.")) for key, _ in local_entries):
            raise VersionControlInitializationError("repository Git configuration is unsupported")

    def _validate_bare_repository(
        self,
        root: Path,
        *,
        coordination_path: Path,
        object_format: GitObjectFormat,
    ) -> None:
        observed_format = self._try_git(
            root,
            "--git-dir",
            str(coordination_path),
            "rev-parse",
            "--show-object-format",
        )
        if observed_format != object_format.value:
            raise VersionControlInitializationError(
                "local coordination repository object format is invalid"
            )
        entries = self._config_entries(
            root,
            "--git-dir",
            str(coordination_path),
        )
        if (
            not entries
            or len(entries) != len({key for key, _ in entries})
            or any(key not in _SAFE_BARE_CONFIG_KEYS for key, _ in entries)
            or dict(entries).get("core.bare") != "true"
        ):
            raise VersionControlInitializationError(
                "local coordination repository configuration is invalid"
            )
        forbidden = (
            coordination_path / "objects" / "info" / "alternates",
            coordination_path / "objects" / "info" / "http-alternates",
            coordination_path / "commondir",
            coordination_path / "gitdir",
        )
        if any(path.exists() or path.is_symlink() for path in forbidden):
            raise VersionControlInitializationError(
                "local coordination repository storage is invalid"
            )
        hooks = coordination_path / "hooks"
        if hooks.is_symlink() or not hooks.is_dir():
            raise VersionControlInitializationError(
                "local coordination repository storage is invalid"
            )
        try:
            hook_entries = tuple(os.scandir(hooks))
        except OSError:
            raise VersionControlInitializationError(
                "local coordination repository storage is invalid"
            ) from None
        if any(
            entry.is_symlink()
            or not entry.is_file(follow_symlinks=False)
            or not entry.name.endswith(".sample")
            for entry in hook_entries
        ):
            raise VersionControlInitializationError(
                "local coordination repository hooks are invalid"
            )
        self._validate_managed_tree(coordination_path)

    def _config_entries(
        self,
        root: Path,
        *prefix: str,
        local_only: bool = True,
    ) -> tuple[tuple[str, str], ...]:
        output = self._git_output(
            root,
            *prefix,
            "config",
            *(("--local",) if local_only else ()),
            "--no-includes",
            "--null",
            "--list",
        )
        entries: list[tuple[str, str]] = []
        for item in output.split("\0"):
            if not item:
                continue
            key, separator, value = item.partition("\n")
            if not separator or not key:
                raise VersionControlInitializationError("repository Git configuration is invalid")
            entries.append((key, value))
        return tuple(entries)

    @staticmethod
    def _validate_managed_tree(root: Path) -> None:
        required_files = (root / "HEAD", root / "config")
        required_directories = (root / "hooks", root / "objects", root / "refs")
        if any(path.is_symlink() or not path.is_file() for path in required_files) or any(
            path.is_symlink() or not path.is_dir() for path in required_directories
        ):
            raise VersionControlInitializationError(
                "local coordination repository storage is invalid"
            )
        entries_seen = 0
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                entries = tuple(os.scandir(current))
            except OSError:
                raise VersionControlInitializationError(
                    "local coordination repository storage is invalid"
                ) from None
            entries_seen += len(entries)
            if entries_seen > _MANAGED_TREE_MAX_ENTRIES:
                raise VersionControlInitializationError(
                    "local coordination repository storage is too large"
                )
            for entry in entries:
                if entry.is_symlink():
                    raise VersionControlInitializationError(
                        "local coordination repository storage is invalid"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))

    def _ensure_bare_head(
        self,
        root: Path,
        *,
        coordination_path: Path,
        branch: str,
    ) -> bool:
        refs = self._git_output(
            root,
            "--git-dir",
            str(coordination_path),
            "for-each-ref",
            "--count=1",
            "--format=%(refname)",
            "refs/heads",
        )
        if refs:
            return False
        expected = f"refs/heads/{branch}"
        current = self._try_git(
            root,
            "--git-dir",
            str(coordination_path),
            "symbolic-ref",
            "HEAD",
        )
        if current == expected:
            return False
        self._git_output(
            root,
            "--git-dir",
            str(coordination_path),
            "symbolic-ref",
            "HEAD",
            expected,
            capture=False,
        )
        return True

    def _seed_branch(
        self,
        root: Path,
        *,
        coordination_path: Path,
        branch: str,
        head_sha: str,
    ) -> bool:
        ref = f"refs/heads/{branch}"
        current = self._try_git(
            root,
            "--git-dir",
            str(coordination_path),
            "rev-parse",
            "--verify",
            ref,
        )
        if current == head_sha:
            return False
        self._git_output(
            root,
            "-c",
            f"core.hooksPath={os.devnull}",
            "push",
            "--porcelain",
            "--no-verify",
            str(coordination_path),
            f"{head_sha}:{ref}",
            capture=False,
        )
        updated = self._try_git(
            root,
            "--git-dir",
            str(coordination_path),
            "rev-parse",
            "--verify",
            ref,
        )
        if not hmac.compare_digest(updated or "", head_sha):
            raise VersionControlInitializationError("local coordination branch verification failed")
        return True

    @staticmethod
    def _write_manifest(path: Path, content: bytes) -> None:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise VersionControlInitializationError("managed version-control state is invalid")
        temporary_path: Path | None = None
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".version-control.",
                suffix=".tmp",
                dir=path.parent,
            )
            temporary_path = Path(temporary)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
        except OSError:
            raise VersionControlInitializationError(
                "version-control manifest could not be recorded"
            ) from None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _try_git(self, cwd: Path, *arguments: str) -> str | None:
        returncode, output = self._run_git(cwd, arguments, capture=True)
        return output if returncode == 0 else None

    def _git_output(
        self,
        cwd: Path,
        *arguments: str,
        capture: bool = True,
    ) -> str:
        returncode, output = self._run_git(cwd, arguments, capture=capture)
        if returncode != 0:
            raise VersionControlInitializationError("Git version-control initialization failed")
        return output

    def _run_git(
        self,
        cwd: Path,
        arguments: tuple[str, ...],
        *,
        capture: bool,
    ) -> tuple[int, str]:
        environment = os.environ.copy()
        for key in tuple(environment):
            if key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
                environment.pop(key)
        for key in (
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_ASKPASS",
            "GIT_COMMON_DIR",
            "GIT_CONFIG",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
            "GIT_DIR",
            "GIT_EDITOR",
            "GIT_EXEC_PATH",
            "GIT_EXTERNAL_DIFF",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_PAGER",
            "GIT_PROXY_COMMAND",
            "GIT_SEQUENCE_EDITOR",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_TEMPLATE_DIR",
            "GIT_WORK_TREE",
            "SSH_ASKPASS",
        ):
            environment.pop(key, None)
        environment["GIT_ALLOW_PROTOCOL"] = "file"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_SYSTEM"] = os.devnull
        environment["GIT_PROTOCOL_FROM_USER"] = "0"
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["LC_ALL"] = "C"
        with tempfile.TemporaryFile() as output:
            try:
                completed = subprocess.run(
                    [self._git, *arguments],
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=output if capture else subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    shell=False,
                    timeout=_COMMAND_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise VersionControlInitializationError(
                    "Git version-control initialization failed"
                ) from None
            if not capture:
                return completed.returncode, ""
            size = output.tell()
            if size > _COMMAND_OUTPUT_MAX_BYTES:
                raise VersionControlInitializationError("Git command output is too large")
            output.seek(0)
            try:
                text = output.read().decode("utf-8")
            except UnicodeDecodeError:
                raise VersionControlInitializationError("Git command output is invalid") from None
        return completed.returncode, text.rstrip("\r\n")
