"""Filesystem driver decorators for scoped and read-only views."""

from __future__ import annotations

from datetime import datetime
from io import BufferedIOBase

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
)
from af.filesystem.manager import FilesystemDriver
from af.framework.errors import PlatformReason

__all__ = ("ReadOnlyFilesystemDriver", "ScopedFilesystemDriver")


class ScopedFilesystemDriver:
    """Expose one logical sub-tree of an underlying disk as its own root."""

    def __init__(self, driver: FilesystemDriver, scope: FilesystemKey) -> None:
        self._driver = driver
        self._scope = scope

    def _scoped_key(self, key: FilesystemKey) -> FilesystemKey:
        return FilesystemKey(f"{self._scope.value}/{key.value}")

    def _scoped_prefix(self, prefix: FilesystemKey | None) -> FilesystemKey:
        if prefix is None:
            return self._scope
        return self._scoped_key(prefix)

    def _relative_key(self, key: FilesystemKey) -> FilesystemKey:
        scoped = self._scope.value
        if key.value == scoped:
            raise FilesystemOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="filesystem.scope.key",
                protected_cause=RuntimeError("filesystem entry escaped scoped namespace"),
            )
        prefix = f"{scoped}/"
        if not key.value.startswith(prefix):
            raise FilesystemOperationError(
                reason=PlatformReason.INTEGRITY_FAILURE,
                stage="filesystem.scope.key",
                protected_cause=RuntimeError("filesystem entry escaped scoped namespace"),
            )
        return FilesystemKey(key.value[len(prefix) :])

    def _relative_metadata(self, metadata: FilesystemMetadata) -> FilesystemMetadata:
        return FilesystemMetadata(
            key=self._relative_key(metadata.key),
            size=metadata.size,
            updated_at=metadata.updated_at,
            checksum_sha256=metadata.checksum_sha256,
            visibility=metadata.visibility,
            version_id=metadata.version_id,
        )

    def read(
        self,
        key: FilesystemKey,
        /,
        *,
        byte_range: ByteRange | None = None,
    ) -> BufferedIOBase:
        return self._driver.read(self._scoped_key(key), byte_range=byte_range)

    def write(
        self,
        key: FilesystemKey,
        stream: BufferedIOBase,
        /,
        *,
        visibility: FileVisibility = FileVisibility.PRIVATE,
    ) -> FilesystemMetadata:
        metadata = self._driver.write(self._scoped_key(key), stream, visibility=visibility)
        return self._relative_metadata(metadata)

    def list(self, prefix: FilesystemKey | None = None, /) -> tuple[FilesystemEntry, ...]:
        entries = self._driver.list(self._scoped_prefix(prefix))
        return tuple(
            FilesystemEntry(
                key=self._relative_key(entry.key),
                size=entry.size,
                updated_at=entry.updated_at,
            )
            for entry in entries
        )

    def metadata(self, key: FilesystemKey, /) -> FilesystemMetadata:
        return self._relative_metadata(self._driver.metadata(self._scoped_key(key)))

    def checksum(self, key: FilesystemKey, /) -> str:
        return self._driver.checksum(self._scoped_key(key))

    def copy(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        self._driver.copy(self._scoped_key(source), self._scoped_key(destination))

    def move(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        self._driver.move(self._scoped_key(source), self._scoped_key(destination))

    def delete(self, key: FilesystemKey, /) -> bool:
        return self._driver.delete(self._scoped_key(key))

    def signed_url(
        self,
        key: FilesystemKey,
        /,
        *,
        expires_at: datetime,
        operation: SignedUrlOperation = SignedUrlOperation.GET,
    ) -> SignedUrl:
        return self._driver.signed_url(
            self._scoped_key(key),
            expires_at=expires_at,
            operation=operation,
        )

    def set_visibility(
        self,
        key: FilesystemKey,
        visibility: FileVisibility,
        /,
    ) -> FilesystemMetadata:
        metadata = self._driver.set_visibility(self._scoped_key(key), visibility)
        return self._relative_metadata(metadata)

    def create_multipart_upload(self, key: FilesystemKey, /) -> MultipartUpload:
        upload = self._driver.create_multipart_upload(self._scoped_key(key))
        return MultipartUpload(key=self._relative_key(upload.key), upload_id=upload.upload_id)

    def upload_part(
        self,
        upload: MultipartUpload,
        part_number: int,
        stream: BufferedIOBase,
        /,
    ) -> MultipartPart:
        scoped = MultipartUpload(key=self._scoped_key(upload.key), upload_id=upload.upload_id)
        return self._driver.upload_part(scoped, part_number, stream)

    def complete_multipart_upload(
        self,
        upload: MultipartUpload,
        parts: tuple[MultipartPart, ...],
        /,
    ) -> FilesystemMetadata:
        scoped = MultipartUpload(key=self._scoped_key(upload.key), upload_id=upload.upload_id)
        metadata = self._driver.complete_multipart_upload(scoped, parts)
        return self._relative_metadata(metadata)

    def close(self) -> None:
        self._driver.close()


class ReadOnlyFilesystemDriver:
    """Deny mutations while preserving a readable driver view."""

    def __init__(self, driver: FilesystemDriver) -> None:
        self._driver = driver

    @staticmethod
    def _readonly_failure(stage: str) -> FilesystemOperationError:
        return FilesystemOperationError(
            reason=PlatformReason.AUTHORIZATION_DENIED,
            stage=stage,
            protected_cause=PermissionError("filesystem driver is read-only"),
        )

    def read(
        self,
        key: FilesystemKey,
        /,
        *,
        byte_range: ByteRange | None = None,
    ) -> BufferedIOBase:
        return self._driver.read(key, byte_range=byte_range)

    def write(
        self,
        key: FilesystemKey,
        stream: BufferedIOBase,
        /,
        *,
        visibility: FileVisibility = FileVisibility.PRIVATE,
    ) -> FilesystemMetadata:
        del key, stream, visibility
        raise self._readonly_failure("filesystem.read-only.write")

    def list(self, prefix: FilesystemKey | None = None, /) -> tuple[FilesystemEntry, ...]:
        return self._driver.list(prefix)

    def metadata(self, key: FilesystemKey, /) -> FilesystemMetadata:
        return self._driver.metadata(key)

    def checksum(self, key: FilesystemKey, /) -> str:
        return self._driver.checksum(key)

    def copy(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        del source, destination
        raise self._readonly_failure("filesystem.read-only.copy")

    def move(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        del source, destination
        raise self._readonly_failure("filesystem.read-only.move")

    def delete(self, key: FilesystemKey, /) -> bool:
        del key
        raise self._readonly_failure("filesystem.read-only.delete")

    def signed_url(
        self,
        key: FilesystemKey,
        /,
        *,
        expires_at: datetime,
        operation: SignedUrlOperation = SignedUrlOperation.GET,
    ) -> SignedUrl:
        if operation is not SignedUrlOperation.GET:
            raise self._readonly_failure("filesystem.read-only.signed-url")
        return self._driver.signed_url(key, expires_at=expires_at, operation=operation)

    def set_visibility(
        self,
        key: FilesystemKey,
        visibility: FileVisibility,
        /,
    ) -> FilesystemMetadata:
        del key, visibility
        raise self._readonly_failure("filesystem.read-only.visibility")

    def create_multipart_upload(self, key: FilesystemKey, /) -> MultipartUpload:
        del key
        raise self._readonly_failure("filesystem.read-only.multipart")

    def upload_part(
        self,
        upload: MultipartUpload,
        part_number: int,
        stream: BufferedIOBase,
        /,
    ) -> MultipartPart:
        del upload, part_number, stream
        raise self._readonly_failure("filesystem.read-only.multipart")

    def complete_multipart_upload(
        self,
        upload: MultipartUpload,
        parts: tuple[MultipartPart, ...],
        /,
    ) -> FilesystemMetadata:
        del upload, parts
        raise self._readonly_failure("filesystem.read-only.multipart")

    def close(self) -> None:
        self._driver.close()
