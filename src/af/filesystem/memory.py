"""Verified in-memory filesystem driver."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BufferedIOBase, BytesIO
from pathlib import Path
from threading import RLock

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
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import (
    CapabilityBinding,
    CapabilityEvidence,
    CapabilitySet,
    ConnectionRef,
    DriverManifest,
    DriverRegistration,
    HealthStatus,
)

__all__ = ("MemoryFilesystemDriver", "memory_filesystem_registration")

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
    )
)
_ARTIFACT_DIGEST = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_DEPENDENCY_DIGEST = hashlib.sha256(b"python:stdlib:memory-filesystem:v1").hexdigest()
_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class _Entry:
    data: bytes
    metadata: FilesystemMetadata


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


class MemoryFilesystemDriver:
    """Process-local filesystem using canonical logical keys."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        if not callable(self._clock):
            raise TypeError("memory filesystem clock must be callable")
        self._entries: dict[FilesystemKey, _Entry] = {}
        self._lock = RLock()
        self._closed = False

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.utcoffset() is None:
            raise PlatformError(
                PlatformReason.PERMANENT_FAILURE,
                stage="filesystem.memory.clock",
            )
        return value.astimezone(UTC)

    def _assert_open(self) -> None:
        if self._closed:
            raise PlatformError(
                PlatformReason.SHUTDOWN_FAILED,
                stage="filesystem.memory.connection",
            )

    @staticmethod
    def _validated_stream(stream: object) -> BufferedIOBase:
        if not isinstance(stream, BufferedIOBase):
            raise ValueError("filesystem write requires a readable binary stream")
        return stream

    @staticmethod
    def _read_all(stream: BufferedIOBase) -> tuple[bytes, str]:
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while True:
            chunk = stream.read(_CHUNK_SIZE)
            if chunk == b"":
                break
            if type(chunk) is not bytes:
                raise ValueError("filesystem stream must yield bytes")
            digest.update(chunk)
            chunks.append(chunk)
        return (b"".join(chunks), digest.hexdigest())

    @staticmethod
    def _entry(key: FilesystemKey, data: bytes, *, updated_at: datetime) -> _Entry:
        checksum = hashlib.sha256(data).hexdigest()
        return _Entry(
            data=data,
            metadata=FilesystemMetadata(
                key=key,
                size=len(data),
                updated_at=updated_at,
                checksum_sha256=checksum,
            ),
        )

    def _required_entry(self, key: FilesystemKey) -> _Entry:
        entry = self._entries.get(key)
        if entry is None:
            raise FilesystemOperationError(
                stage="filesystem.memory.lookup",
                protected_cause=FileNotFoundError(key.value),
            )
        return entry

    def read(self, key: FilesystemKey, /, *, byte_range: ByteRange | None = None) -> BytesIO:
        with self._lock:
            self._assert_open()
            entry = self._required_entry(key)
            data = entry.data
            if byte_range is not None:
                start, end = byte_range.bounds(len(data))
                data = data[start:end]
            return BytesIO(data)

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
        validated = self._validated_stream(stream)
        data, _checksum = self._read_all(validated)
        with self._lock:
            self._assert_open()
            entry = self._entry(key, data, updated_at=self._now())
            self._entries[key] = entry
            return entry.metadata

    def list(self, prefix: FilesystemKey | None = None, /) -> tuple[FilesystemEntry, ...]:
        with self._lock:
            self._assert_open()
            prefix_value = None if prefix is None else prefix.value
            entries = [
                FilesystemEntry(
                    key=entry.metadata.key,
                    size=entry.metadata.size,
                    updated_at=entry.metadata.updated_at,
                )
                for entry in self._entries.values()
                if prefix_value is None
                or entry.metadata.key.value == prefix_value
                or entry.metadata.key.value.startswith(prefix_value + "/")
            ]
            return tuple(sorted(entries, key=lambda item: item.key.value))

    def metadata(self, key: FilesystemKey, /) -> FilesystemMetadata:
        with self._lock:
            self._assert_open()
            return self._required_entry(key).metadata

    def checksum(self, key: FilesystemKey, /) -> str:
        return self.metadata(key).checksum_sha256

    def copy(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        with self._lock:
            self._assert_open()
            entry = self._required_entry(source)
            self._entries[destination] = self._entry(
                destination,
                entry.data,
                updated_at=self._now(),
            )

    def move(self, source: FilesystemKey, destination: FilesystemKey, /) -> None:
        with self._lock:
            self._assert_open()
            entry = self._required_entry(source)
            self._entries[destination] = self._entry(
                destination,
                entry.data,
                updated_at=self._now(),
            )
            self._entries.pop(source, None)

    def delete(self, key: FilesystemKey, /) -> bool:
        with self._lock:
            self._assert_open()
            return self._entries.pop(key, None) is not None

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
        with self._lock:
            if self._closed:
                return
            self._entries.clear()
            self._closed = True


def memory_filesystem_registration(
    *,
    clock: Callable[[], datetime] | None = None,
) -> DriverRegistration[FilesystemDriver]:
    """Return an inert memory-filesystem declaration with runtime evidence."""

    evidence_clock = clock if clock is not None else (lambda: datetime.now(UTC))
    manifest = DriverManifest(
        driver_name="memory",
        version="1",
        artifact_digest=_ARTIFACT_DIGEST,
        dependency_graph_digest=_DEPENDENCY_DIGEST,
        capabilities=_CAPABILITIES,
    )

    def factory(ref: ConnectionRef, config: Mapping[str, object]) -> FilesystemDriver:
        if (
            ref.manager_id != "filesystem"
            or ref.driver_name != "memory"
            or set(config) != {"driver"}
            or config.get("driver") != "memory"
        ):
            raise ValueError("memory filesystem connection configuration is invalid")
        return MemoryFilesystemDriver(clock=evidence_clock)

    def verify(
        ref: ConnectionRef,
        driver: FilesystemDriver,
        expected: CapabilityBinding,
    ) -> CapabilityEvidence:
        if (
            ref.manager_id != "filesystem"
            or ref.driver_name != "memory"
            or not isinstance(driver, MemoryFilesystemDriver)
        ):
            raise ValueError("memory filesystem runtime capability verification failed")
        observed_at = evidence_clock()
        if not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("memory filesystem evidence clock must be timezone-aware")
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
        driver_name="memory",
        factory=factory,
        manifest=manifest,
        verifier=verify,
        disposer=lambda driver: driver.close(),
    )
