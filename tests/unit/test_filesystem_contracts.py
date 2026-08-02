from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from af.filesystem.contracts import (
    ByteRange,
    FilesystemEntry,
    FilesystemKey,
    FilesystemMetadata,
    FileVisibility,
    MultipartPart,
    MultipartUpload,
    SignedUrl,
    SignedUrlOperation,
    UnsupportedFilesystemFeature,
)
from af.framework.errors import PlatformReason


def test_filesystem_key_normalizes_separators_and_rejects_escape() -> None:
    assert FilesystemKey(r"nested\\docs/./report.txt").value == "nested/docs/report.txt"

    for value in ("", "/absolute/path", "../escape.txt", "nested/../escape.txt", "C:/drive"):
        with pytest.raises(ValueError):
            FilesystemKey(value)


def test_byte_range_is_bounded_and_inclusive() -> None:
    byte_range = ByteRange(start=2, end=4)

    assert byte_range.bounds(10) == (2, 5)
    assert ByteRange(start=6).bounds(11) == (6, 11)
    assert ByteRange(start=20).bounds(5) == (5, 5)

    with pytest.raises(ValueError):
        ByteRange(start=-1)
    with pytest.raises(ValueError):
        ByteRange(start=4, end=3)


def test_filesystem_contract_records_are_immutable_and_validated() -> None:
    observed_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    key = FilesystemKey("docs/report.txt")
    entry = FilesystemEntry(key=key, size=5, updated_at=observed_at)
    metadata = FilesystemMetadata(
        key=key,
        size=5,
        updated_at=observed_at,
        checksum_sha256="a" * 64,
        visibility=FileVisibility.PRIVATE,
    )
    signed_url = SignedUrl(
        operation=SignedUrlOperation.GET,
        url="https://example.invalid/download",
        expires_at=observed_at + timedelta(minutes=5),
    )
    upload = MultipartUpload(key=key, upload_id="upload-1")
    part = MultipartPart(part_number=1, checksum_sha256="b" * 64)

    assert entry.key == key
    assert metadata.visibility is FileVisibility.PRIVATE
    assert signed_url.operation is SignedUrlOperation.GET
    assert upload.key == key
    assert part.part_number == 1

    with pytest.raises(ValueError):
        FilesystemMetadata(
            key=key,
            size=-1,
            updated_at=observed_at,
            checksum_sha256="a" * 64,
        )
    with pytest.raises(ValueError):
        SignedUrl(
            operation=SignedUrlOperation.PUT,
            url="",
            expires_at=observed_at,
        )


def test_filesystem_contracts_fail_closed_for_invalid_types_and_bounds() -> None:
    observed_at = datetime(2026, 8, 1, 12, tzinfo=UTC)
    key = FilesystemKey("docs/report.txt")

    with pytest.raises(ValueError):
        ByteRange(start=0).bounds(-1)
    with pytest.raises(ValueError):
        FilesystemKey("/".join(["a" * 255] * 9))
    with pytest.raises(ValueError):
        FilesystemKey("a" * 256)
    with pytest.raises(ValueError):
        FilesystemKey(123)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FilesystemEntry(key="docs/report.txt", size=1, updated_at=observed_at)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FilesystemEntry(key=key, size=1, updated_at=datetime(2026, 8, 1, 12))
    with pytest.raises(ValueError):
        FilesystemMetadata(
            key="docs/report.txt",  # type: ignore[arg-type]
            size=1,
            updated_at=observed_at,
            checksum_sha256="a" * 64,
        )
    with pytest.raises(ValueError):
        FilesystemMetadata(
            key=key,
            size=1,
            updated_at=observed_at,
            checksum_sha256="a" * 64,
            visibility="public",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        SignedUrl(
            operation="get",  # type: ignore[arg-type]
            url="https://example.invalid/download",
            expires_at=observed_at,
        )
    with pytest.raises(ValueError):
        MultipartUpload(key="docs/report.txt", upload_id="upload-1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        MultipartUpload(key=key, upload_id="bad space")
    with pytest.raises(ValueError):
        MultipartPart(part_number=0, checksum_sha256="b" * 64)


def test_unsupported_filesystem_feature_reports_capability_reason() -> None:
    error = UnsupportedFilesystemFeature("signed-urls")

    assert error.feature == "signed-urls"
    assert error.reason is PlatformReason.CAPABILITY_UNAVAILABLE
