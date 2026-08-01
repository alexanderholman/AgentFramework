"""Verified local filesystem driver with bounded root isolation."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from io import BufferedIOBase
from pathlib import Path

from af.filesystem.contracts import (
    ByteRange,
    FilesystemEntry,
    FilesystemKey,
    FilesystemMetadata,
    FilesystemOperationError,
    FileVisibility,
    MultipartPart,
    MultipartUpload,
    SignedUrl,
    SignedUrlOperation,
    UnsupportedFilesystemFeature,
)
from af.filesystem.manager import FilesystemDriver, FilesystemFeature
from af.framework.errors import PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)

__all__ = ("LocalFilesystemDriver", "local_filesystem_registration")

_CAPABILITIES = CapabilitySet(
    (
        FilesystemFeature.READ.value,
        FilesystemFeature.WRITE.value,
        FilesystemFeature.LIST.value,
        FilesystemFeature.METADATA.value,
        FilesystemFeature.CHECKSUM.value,
        FilesystemFeature.RANGE.value,
        FilesystemFeature.COPY.value,
        FilesystemFeature.MOVE.value,
        FilesystemFeature.DELETE.value,
        FilesystemFeature.STREAMING.value,
    )
)
_ARTIFACT_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(
    f"{platform.python_implementation()}:{platform.python_version()}:local-filesystem:v1".encode()
).hexdigest()
_CHUNK_SIZE = 64 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS |= getattr(os, "O_NOFOLLOW", 0)


def _evidence_digest(
    binding: CapabilityBinding,
    capabilities: CapabilitySet,
    observed_at: datetime,
) -> str:
    payload = {
        "artifact": binding.artifact_digest,
        "capabilities": tuple(capabilities),
        "configuration": binding.configuration_digest,
        "dependencies": binding.dependency_graph_digest,
        "observed_at": observed_at.isoformat(),
        "policy": binding.policy_version,
        "profile": binding.deployment_profile,
        "version": binding.driver_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class _BoundedFileStream(BufferedIOBase):
    def __init__(self, handle: BufferedIOBase, remaining: int) -> None:
        self._handle = handle
        self._remaining = remaining

    def readable(self) -> bool:
        return True

    def read(self, size: int | None = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        limit = self._remaining if size is None or size < 0 else min(size, self._remaining)
        chunk = self._handle.read(limit)
        if type(chunk) is not bytes:
            raise ValueError("filesystem stream must yield bytes")
        self._remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self._handle.close()
        super().close()

    def __enter__(self) -> _BoundedFileStream:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()
        return None


class LocalFilesystemDriver:
    """Root-bounded local disk with traversal and symlink escape protection."""

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(root, Path):
            candidate = root
        elif type(root) is str and root:
            candidate = Path(root)
        else:
            raise ValueError("local filesystem root is invalid")
        if not candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("local filesystem root must be an absolute canonical path")
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        resolved = candidate.resolve(strict=True)
        if resolved != candidate or not resolved.is_dir():
            raise ValueError("local filesystem root must be an absolute canonical directory")
        self._root = resolved
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("local filesystem clock must be callable")
        self._closed = False

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise FilesystemOperationError(
                reason=PlatformReason.PERMANENT_FAILURE,
                stage="filesystem.local.clock",
            )
        return value.astimezone(UTC)

    def _assert_open(self) -> None:
        if self._closed:
            raise FilesystemOperationError(
                reason=PlatformReason.SHUTDOWN_FAILED,
                stage="filesystem.local.connection",
            )

    def _root_resolved(self) -> Path:
        return self._root

    def _path(self, key: FilesystemKey) -> Path:
        root = self._root_resolved()
        candidate = self._root / key.value
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as error:
            raise FilesystemOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="filesystem.local.path",
                protected_cause=error,
            ) from error
        if not resolved.is_relative_to(root):
            raise FilesystemOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="filesystem.local.path",
                protected_cause=RuntimeError("filesystem path escaped root"),
            )
        return candidate

    def _open_directory(self, path: Path, *, create: bool) -> int:
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise FilesystemOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="filesystem.local.path",
            )
        try:
            descriptor = os.open(path, _DIRECTORY_FLAGS)
        except OSError as error:
            raise FilesystemOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="filesystem.local.path",
                protected_cause=error,
            ) from error
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISDIR(status.st_mode):
                raise FilesystemOperationError(
                    reason=PlatformReason.INTEGRITY_FAILURE,
                    stage="filesystem.local.path",
                )
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _walk_parent_directory(self, key: FilesystemKey, *, create: bool) -> tuple[int, str]:
        descriptor = self._open_directory(self._root, create=False)
        parts = key.value.split("/")
        try:
            for part in parts[:-1]:
                try:
                    next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                        next_descriptor = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    except OSError as error:
                        raise FilesystemOperationError(
                            reason=PlatformReason.INTEGRITY_FAILURE,
                            stage="filesystem.local.path",
                            protected_cause=error,
                        ) from error
                except OSError as error:
                    raise FilesystemOperationError(
                        reason=PlatformReason.INTEGRITY_FAILURE,
                        stage="filesystem.local.path",
                        protected_cause=error,
                    ) from error
                os.close(descriptor)
                descriptor = next_descriptor
            return (descriptor, parts[-1])
        except FileNotFoundError as error:
            os.close(descriptor)
            raise FilesystemOperationError(
                stage="filesystem.local.path",
                protected_cause=error,
            ) from error
        except Exception:
            os.close(descriptor)
            raise

    @staticmethod
    def _validated_stream(stream: object) -> BufferedIOBase:
        if not isinstance(stream, BufferedIOBase):
            raise ValueError("filesystem write requires a readable binary stream")
        return stream

    def _checksum(self, path: Path) -> tuple[int, str]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_SIZE)
                if chunk == b"":
                    break
                digest.update(chunk)
                size += len(chunk)
        return (size, digest.hexdigest())

    def _metadata(self, key: FilesystemKey, path: Path) -> FilesystemMetadata:
        try:
            stat = path.stat()
        except FileNotFoundError as error:
            raise FilesystemOperationError(
                stage="filesystem.local.metadata",
                protected_cause=error,
            ) from error
        size, checksum = self._checksum(path)
        return FilesystemMetadata(
            key=key,
            size=size,
            updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            checksum_sha256=checksum,
        )

    def read(
        self,
        key: FilesystemKey,
        /,
        *,
        byte_range: ByteRange | None = None,
    ) -> BufferedIOBase:
        self._assert_open()
        path = self._path(key)
        try:
            handle = path.open("rb")
        except FileNotFoundError as error:
            raise FilesystemOperationError(
                stage="filesystem.local.read",
                protected_cause=error,
            ) from error
        if byte_range is None:
            return handle
        try:
            size = os.fstat(handle.fileno()).st_size
            start, end = byte_range.bounds(size)
            handle.seek(start)
            return _BoundedFileStream(handle, end - start)
        except Exception as error:
            handle.close()
            if isinstance(error, FilesystemOperationError):
                raise
            raise FilesystemOperationError(
                stage="filesystem.local.read",
                protected_cause=error,
            ) from error

    def write(
        self,
        key: FilesystemKey,
        stream: BufferedIOBase,
        /,
        *,
        visibility: FileVisibility = FileVisibility.PRIVATE,
    ) -> FilesystemMetadata:
        if visibility is not FileVisibility.PRIVATE:
            raise UnsupportedFilesystemFeature(FilesystemFeature.VISIBILITY.value)
        self._assert_open()
        validated = self._validated_stream(stream)
        path = self._path(key)
        parent_descriptor, leaf_name = self._walk_parent_directory(key, create=True)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp_handle:
                temp_name = temp_handle.name
                while True:
                    chunk = validated.read(_CHUNK_SIZE)
                    if chunk == b"":
                        break
                    if type(chunk) is not bytes:
                        raise ValueError("filesystem stream must yield bytes")
                    temp_handle.write(chunk)
            replacement_name = Path(temp_name).name
            os.replace(
                replacement_name,
                leaf_name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
        except FilesystemOperationError:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)
            raise
        except Exception as error:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)
            reason = (
                PlatformReason.PERMANENT_FAILURE
                if isinstance(error, ValueError)
                else PlatformReason.INTEGRITY_FAILURE
            )
            raise FilesystemOperationError(
                reason=reason,
                stage="filesystem.local.write",
                protected_cause=error,
            ) from error
        finally:
            os.close(parent_descriptor)
            if temp_name is not None:
                with suppress(OSError):
                    Path(temp_name).unlink(missing_ok=True)
        return self._metadata(key, path)

    def list(self, prefix: FilesystemKey | None = None, /) -> tuple[FilesystemEntry, ...]:
        self._assert_open()
        root = self._root_resolved()
        prefix_value = None if prefix is None else prefix.value
        entries: list[FilesystemEntry] = []
        for current_root, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [name for name in dirnames if not Path(current_root, name).is_symlink()]
            for filename in filenames:
                path = Path(current_root, filename)
                if path.is_symlink():
                    continue
                relative = path.relative_to(root).as_posix()
                key = FilesystemKey(relative)
                if (
                    prefix_value is not None
                    and key.value != prefix_value
                    and not key.value.startswith(prefix_value + "/")
                ):
                    continue
                stat = path.stat()
                entries.append(
                    FilesystemEntry(
                        key=key,
                        size=stat.st_size,
                        updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    )
                )
        return tuple(sorted(entries, key=lambda item: item.key.value))

    def metadata(self, key: FilesystemKey, /) -> FilesystemMetadata:
        self._assert_open()
        path = self._path(key)
        return self._metadata(key, path)

    def checksum(self, key: FilesystemKey, /) -> str:
        return self.metadata(key).checksum_sha256

    def copy(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        self._assert_open()
        source_path = self._path(source)
        with source_path.open("rb") as stream:
            self.write(destination, stream)

    def move(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        self._assert_open()
        source_path = self._path(source)
        destination_path = self._path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(source_path, destination_path)
        except Exception as error:
            raise FilesystemOperationError(
                stage="filesystem.local.move",
                protected_cause=error,
            ) from error

    def delete(self, key: FilesystemKey, /) -> bool:
        self._assert_open()
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        except Exception as error:
            raise FilesystemOperationError(
                stage="filesystem.local.delete",
                protected_cause=error,
            ) from error
        return True

    def signed_url(
        self,
        key: FilesystemKey,
        /,
        *,
        expires_at: datetime,
        operation: SignedUrlOperation = SignedUrlOperation.GET,
    ) -> SignedUrl:
        del key, expires_at, operation
        raise UnsupportedFilesystemFeature(FilesystemFeature.SIGNED_URLS.value)

    def set_visibility(
        self,
        key: FilesystemKey,
        visibility: FileVisibility,
        /,
    ) -> FilesystemMetadata:
        del key, visibility
        raise UnsupportedFilesystemFeature(FilesystemFeature.VISIBILITY.value)

    def create_multipart_upload(self, key: FilesystemKey, /) -> MultipartUpload:
        del key
        raise UnsupportedFilesystemFeature(FilesystemFeature.MULTIPART.value)

    def upload_part(
        self,
        upload: MultipartUpload,
        part_number: int,
        stream: BufferedIOBase,
        /,
    ) -> MultipartPart:
        del upload, part_number, stream
        raise UnsupportedFilesystemFeature(FilesystemFeature.MULTIPART.value)

    def complete_multipart_upload(
        self,
        upload: MultipartUpload,
        parts: tuple[MultipartPart, ...],
        /,
    ) -> FilesystemMetadata:
        del upload, parts
        raise UnsupportedFilesystemFeature(FilesystemFeature.MULTIPART.value)

    def close(self) -> None:
        self._closed = True


def local_filesystem_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[FilesystemDriver]:
    """Return an inert local-filesystem declaration with runtime evidence."""

    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="local",
        version="1",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> FilesystemDriver:
        if (
            ref.manager_id != "filesystem"
            or ref.driver_name != "local"
            or set(config) != {"driver", "root"}
            or config.get("driver") != "local"
            or type(config.get("root")) is not str
            or not config.get("root")
        ):
            raise ValueError("local filesystem connection configuration is invalid")
        return LocalFilesystemDriver(config["root"], clock=evidence_clock)  # type: ignore[arg-type]

    def verify(
        ref: ConnectionRef,
        driver: FilesystemDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "filesystem"
            or ref.driver_name != "local"
            or not isinstance(driver, LocalFilesystemDriver)
        ):
            raise ValueError("local filesystem runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("local filesystem evidence clock must be timezone-aware")
        observed_at = observed_at.astimezone(UTC)
        return CapabilityEvidence(
            binding=expected,
            capabilities=_CAPABILITIES,
            health=HealthStatus.HEALTHY,
            observed_at=observed_at,
            expires_at=observed_at + timedelta(minutes=1),
            evidence_digest=_evidence_digest(expected, _CAPABILITIES, observed_at),
        )

    return DriverRegistration(
        manager_id="filesystem",
        driver_name="local",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
