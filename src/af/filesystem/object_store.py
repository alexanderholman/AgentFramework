"""Bounded object-store IO over explicit vendor SDK ports.

Copy/move are streamed copy and copy-then-delete, never POSIX atomic rename.
Metadata SHA-256 is calculated by streaming content; ETags are not checksums.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BufferedIOBase, BufferedReader, BytesIO, RawIOBase
from pathlib import Path
from typing import Protocol

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
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)

_CHUNK = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    size: int
    updated_at: datetime
    version_id: str | None = None


class ObjectBackend(Protocol):
    def stat(self, key: str) -> ObjectInfo: ...
    def chunks(self, key: str, byte_range: ByteRange | None = None) -> Iterator[bytes]: ...
    def put(self, key: str, chunks: Iterator[bytes]) -> None: ...
    def list(self, prefix: str) -> Iterable[tuple[str, ObjectInfo]]: ...
    def delete(self, key: str) -> None: ...
    def check(self) -> None: ...


class _ChunkReader(RawIOBase):
    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._remaining = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: object) -> int:
        # RawIOBase supplies a writable buffer; memoryview avoids a second object copy.
        view = memoryview(buffer)  # type: ignore[arg-type]
        try:
            while not self._remaining:
                try:
                    self._remaining = memoryview(next(self._chunks))
                except StopIteration:
                    return 0
            count = min(len(view), len(self._remaining))
            view[:count] = self._remaining[:count]
            self._remaining = self._remaining[count:]
            return count
        except Exception as error:
            raise FilesystemOperationError(protected_cause=error) from None

    def close(self) -> None:
        close = getattr(self._chunks, "close", None)
        if close is not None:
            close()
        self._remaining = memoryview(b"")
        super().close()


class ObjectStorageFilesystemDriver:
    def __init__(
        self,
        backend: ObjectBackend,
        *,
        signer: Callable[[str, datetime, SignedUrlOperation], str] | None = None,
    ) -> None:
        self._backend = backend
        self._signer = signer
        self._closed = False

    @property
    def capabilities(self) -> CapabilitySet:
        features = {
            feature.value
            for feature in (
                FilesystemFeature.READ,
                FilesystemFeature.WRITE,
                FilesystemFeature.LIST,
                FilesystemFeature.METADATA,
                FilesystemFeature.CHECKSUM,
                FilesystemFeature.RANGE,
                FilesystemFeature.COPY,
                FilesystemFeature.MOVE,
                FilesystemFeature.DELETE,
                FilesystemFeature.STREAMING,
            )
        }
        if self._signer is not None:
            features.add(FilesystemFeature.SIGNED_URLS.value)
        return CapabilitySet(features)

    def check(self) -> None:
        self._assert_open()
        try:
            self._backend.check()
        except Exception as error:
            raise FilesystemOperationError(protected_cause=error) from None

    def _assert_open(self) -> None:
        if self._closed:
            raise FilesystemOperationError(stage="filesystem.closed")

    def read(self, key: FilesystemKey, /, *, byte_range: ByteRange | None = None) -> BufferedIOBase:
        self._assert_open()
        try:
            info = self._backend.stat(key.value)
            if byte_range is not None and byte_range.start >= info.size:
                return BytesIO()
            return BufferedReader(_ChunkReader(self._backend.chunks(key.value, byte_range)))
        except Exception as error:
            raise FilesystemOperationError(protected_cause=error) from None

    def write(
        self,
        key: FilesystemKey,
        stream: BufferedIOBase,
        /,
        *,
        visibility: FileVisibility = FileVisibility.PRIVATE,
    ) -> FilesystemMetadata:
        self._assert_open()
        if visibility is not FileVisibility.PRIVATE:
            raise UnsupportedFilesystemFeature(FilesystemFeature.VISIBILITY.value)
        digest = hashlib.sha256()
        size = 0

        def chunks() -> Iterator[bytes]:
            nonlocal size
            while data := stream.read(_CHUNK):
                if type(data) is not bytes:
                    raise ValueError("Filesystem writes require binary streams")
                size += len(data)
                digest.update(data)
                yield data

        try:
            source = chunks()
            self._backend.put(key.value, source)
            if next(source, None) is not None:
                raise ValueError("Object backend did not consume the complete stream")
            info = self._backend.stat(key.value)
            if info.size != size:
                raise ValueError("Object size does not match the uploaded stream")
            return FilesystemMetadata(
                key, size, info.updated_at, digest.hexdigest(), version_id=info.version_id
            )
        except Exception as error:
            raise FilesystemOperationError(protected_cause=error) from None

    def list(self, prefix: FilesystemKey | None = None, /) -> tuple[FilesystemEntry, ...]:
        self._assert_open()
        value = "" if prefix is None else prefix.value
        try:
            result = []
            for name, info in self._backend.list(value):
                if name.endswith("/"):
                    continue  # Object-store directory markers are not logical files.
                key = FilesystemKey(name)
                if key.value != name:
                    raise ValueError(
                        "Noncanonical object key cannot enter the filesystem namespace"
                    )
                if not value or name == value or name.startswith(value + "/"):
                    result.append(FilesystemEntry(key, info.size, info.updated_at))
            return tuple(sorted(result, key=lambda entry: entry.key.value))
        except Exception as error:
            raise FilesystemOperationError(protected_cause=error) from None

    def metadata(self, key: FilesystemKey, /) -> FilesystemMetadata:
        self._assert_open()
        try:
            info = self._backend.stat(key.value)
            checksum = self.checksum(key)
            after = self._backend.stat(key.value)
            if after != info:
                raise ValueError("Object changed during metadata inspection")
            return FilesystemMetadata(
                key, info.size, info.updated_at, checksum, version_id=info.version_id
            )
        except Exception as error:
            raise FilesystemOperationError(protected_cause=error) from None

    def checksum(self, key: FilesystemKey, /) -> str:
        digest = hashlib.sha256()
        with self.read(key) as stream:
            while chunk := stream.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def copy(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        if source == destination:
            self.metadata(source)
            return
        with self.read(source) as stream:
            self.write(destination, stream)

    def move(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        if source == destination:
            self.metadata(source)
            return
        self.copy(source, destination)
        self.delete(source)

    def delete(self, key: FilesystemKey, /) -> bool:
        self._assert_open()
        try:
            self._backend.stat(key.value)
            self._backend.delete(key.value)
            return True
        except FileNotFoundError:
            return False
        except Exception as error:
            raise FilesystemOperationError(protected_cause=error) from None

    def signed_url(
        self,
        key: FilesystemKey,
        /,
        *,
        expires_at: datetime,
        operation: SignedUrlOperation = SignedUrlOperation.GET,
    ) -> SignedUrl:
        self._assert_open()
        if self._signer is None:
            raise UnsupportedFilesystemFeature(FilesystemFeature.SIGNED_URLS.value)
        if expires_at.utcoffset() is None or expires_at <= datetime.now(UTC):
            raise ValueError("Signed URL expiry must be a future aware datetime")
        try:
            return SignedUrl(operation, self._signer(key.value, expires_at, operation), expires_at)
        except Exception as error:
            raise FilesystemOperationError(protected_cause=error) from None

    def set_visibility(
        self, key: FilesystemKey, visibility: FileVisibility, /
    ) -> FilesystemMetadata:
        raise UnsupportedFilesystemFeature(FilesystemFeature.VISIBILITY.value)

    def create_multipart_upload(self, key: FilesystemKey, /) -> MultipartUpload:
        raise UnsupportedFilesystemFeature(FilesystemFeature.MULTIPART.value)

    def upload_part(
        self, upload: MultipartUpload, part_number: int, stream: BufferedIOBase, /
    ) -> MultipartPart:
        raise UnsupportedFilesystemFeature(FilesystemFeature.MULTIPART.value)

    def complete_multipart_upload(
        self, upload: MultipartUpload, parts: tuple[MultipartPart, ...], /
    ) -> FilesystemMetadata:
        raise UnsupportedFilesystemFeature(FilesystemFeature.MULTIPART.value)

    def close(self) -> None:
        # Clients are caller-owned, permitting several disks to share a client.
        self._closed = True


def object_filesystem_registration(
    driver_name: str,
    factory: Callable[[Mapping[str, object]], ObjectStorageFilesystemDriver],
    *,
    signed_urls: bool = False,
) -> DriverRegistration[FilesystemDriver]:
    """Register an injected SDK-backed factory, probing connectivity at resolution."""
    declared = {
        feature.value
        for feature in (
            FilesystemFeature.READ,
            FilesystemFeature.WRITE,
            FilesystemFeature.LIST,
            FilesystemFeature.METADATA,
            FilesystemFeature.CHECKSUM,
            FilesystemFeature.RANGE,
            FilesystemFeature.COPY,
            FilesystemFeature.MOVE,
            FilesystemFeature.DELETE,
            FilesystemFeature.STREAMING,
        )
    }
    if signed_urls:
        declared.add(FilesystemFeature.SIGNED_URLS.value)

    def build(ref: ConnectionRef, config: Mapping[str, object]) -> FilesystemDriver:
        if ref.manager_id != "filesystem":
            raise ValueError("Object disks require the filesystem manager")
        return factory(config)

    def verify(
        ref: ConnectionRef, driver: FilesystemDriver, expected: CapabilityBinding
    ) -> CapabilityEvidence:
        if not isinstance(driver, ObjectStorageFilesystemDriver):
            raise ValueError("Expected an object filesystem driver")
        driver.check()
        now = datetime.now(UTC)
        return CapabilityEvidence(
            binding=expected,
            capabilities=driver.capabilities,
            health=HealthStatus.HEALTHY,
            observed_at=now,
            expires_at=now + timedelta(minutes=1),
            evidence_digest=hashlib.sha256(
                repr((expected, tuple(driver.capabilities), now)).encode()
            ).hexdigest(),
        )

    return DriverRegistration(
        manager_id="filesystem",
        driver_name=driver_name,
        factory=build,
        manifest=DriverManifest(
            driver_name,
            "1",
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            hashlib.sha256(b"explicit-object-sdk-port-v1").hexdigest(),
            CapabilitySet(declared),
        ),
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
