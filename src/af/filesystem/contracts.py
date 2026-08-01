"""Driver-neutral filesystem contracts with canonical logical keys."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from af.framework.errors import PlatformError, PlatformReason

__all__ = (
    "ByteRange",
    "FileVisibility",
    "FilesystemEntry",
    "FilesystemKey",
    "FilesystemMetadata",
    "FilesystemOperationError",
    "MultipartPart",
    "MultipartUpload",
    "SignedUrl",
    "SignedUrlOperation",
    "UnsupportedFilesystemFeature",
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")


def _aware_utc(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"filesystem {field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _bounded_text(value: object, field_name: str, *, limit: int = 2048) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > limit
        or _CONTROL.search(value) is not None
    ):
        raise ValueError(f"filesystem {field_name} must be bounded non-empty text")
    return value


def _sha256(value: object, field_name: str) -> str:
    text = _bounded_text(value, field_name, limit=64)
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"filesystem {field_name} must be a lowercase SHA-256 digest")
    return text


class FileVisibility(StrEnum):
    PRIVATE = "private"
    PUBLIC = "public"


class SignedUrlOperation(StrEnum):
    GET = "get"
    PUT = "put"


class FilesystemOperationError(PlatformError):
    """Sanitized filesystem operation failure."""

    def __init__(
        self,
        *,
        reason: PlatformReason = PlatformReason.PERMANENT_FAILURE,
        stage: str = "filesystem.operation",
        protected_cause: BaseException | None = None,
    ) -> None:
        super().__init__(reason, stage=stage, protected_cause=protected_cause)


class UnsupportedFilesystemFeature(FilesystemOperationError):
    """A requested feature is not supported by the selected disk."""

    def __init__(self, feature: str) -> None:
        self.feature = feature
        super().__init__(
            reason=PlatformReason.CAPABILITY_UNAVAILABLE,
            stage="filesystem.capability",
        )


@dataclass(frozen=True, slots=True)
class FilesystemKey:
    """Canonical logical path relative to a disk root."""

    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str:
            raise ValueError("filesystem key must be a string")
        if self.value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", self.value):
            raise ValueError("filesystem key must be relative")
        normalized = self.value.replace("\\", "/")
        if not normalized or _CONTROL.search(normalized) is not None:
            raise ValueError("filesystem key must be bounded non-empty text")
        parts: list[str] = []
        for part in normalized.split("/"):
            if part in {"", "."}:
                continue
            if part == "..":
                raise ValueError("filesystem key must not escape its namespace")
            if len(part) > 255:
                raise ValueError("filesystem key segment is too long")
            parts.append(part)
        if not parts:
            raise ValueError("filesystem key must identify a file or prefix")
        object.__setattr__(self, "value", "/".join(parts))


@dataclass(frozen=True, slots=True)
class ByteRange:
    """Inclusive byte range over one object."""

    start: int
    end: int | None = None

    def __post_init__(self) -> None:
        if type(self.start) is not int or self.start < 0:
            raise ValueError("filesystem range start is invalid")
        if self.end is not None and (type(self.end) is not int or self.end < self.start):
            raise ValueError("filesystem range end is invalid")

    def bounds(self, size: int) -> tuple[int, int]:
        if type(size) is not int or size < 0:
            raise ValueError("filesystem size is invalid")
        if self.start >= size:
            return (size, size)
        if self.end is None:
            return (self.start, size)
        return (self.start, min(size, self.end + 1))


@dataclass(frozen=True, slots=True)
class FilesystemEntry:
    key: FilesystemKey
    size: int
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.key, FilesystemKey):
            raise ValueError("filesystem entry key is invalid")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("filesystem entry size is invalid")
        object.__setattr__(self, "updated_at", _aware_utc(self.updated_at, "entry update time"))


@dataclass(frozen=True, slots=True)
class FilesystemMetadata:
    key: FilesystemKey
    size: int
    updated_at: datetime
    checksum_sha256: str
    visibility: FileVisibility = FileVisibility.PRIVATE
    version_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, FilesystemKey):
            raise ValueError("filesystem metadata key is invalid")
        if type(self.size) is not int or self.size < 0:
            raise ValueError("filesystem metadata size is invalid")
        object.__setattr__(self, "updated_at", _aware_utc(self.updated_at, "metadata update time"))
        object.__setattr__(self, "checksum_sha256", _sha256(self.checksum_sha256, "checksum"))
        if not isinstance(self.visibility, FileVisibility):
            raise ValueError("filesystem visibility is invalid")
        if self.version_id is not None:
            object.__setattr__(
                self,
                "version_id",
                _bounded_text(self.version_id, "version id", limit=255),
            )


@dataclass(frozen=True, slots=True)
class SignedUrl:
    operation: SignedUrlOperation
    url: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.operation, SignedUrlOperation):
            raise ValueError("filesystem signed URL operation is invalid")
        object.__setattr__(self, "url", _bounded_text(self.url, "signed URL", limit=4096))
        object.__setattr__(self, "expires_at", _aware_utc(self.expires_at, "signed URL expiry"))


@dataclass(frozen=True, slots=True)
class MultipartUpload:
    key: FilesystemKey
    upload_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.key, FilesystemKey):
            raise ValueError("filesystem multipart key is invalid")
        if type(self.upload_id) is not str or _UPLOAD_ID.fullmatch(self.upload_id) is None:
            raise ValueError("filesystem multipart upload id is invalid")


@dataclass(frozen=True, slots=True)
class MultipartPart:
    part_number: int
    checksum_sha256: str

    def __post_init__(self) -> None:
        if type(self.part_number) is not int or not 1 <= self.part_number <= 10_000:
            raise ValueError("filesystem multipart part number is invalid")
        object.__setattr__(self, "checksum_sha256", _sha256(self.checksum_sha256, "checksum"))
