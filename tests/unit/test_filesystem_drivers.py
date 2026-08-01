from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BufferedIOBase, BytesIO
from pathlib import Path

import pytest

from af.filesystem.contracts import (
    FilesystemKey,
    FilesystemOperationError,
    FileVisibility,
    MultipartPart,
    MultipartUpload,
    UnsupportedFilesystemFeature,
)
from af.filesystem.local import LocalFilesystemDriver, local_filesystem_registration
from af.filesystem.memory import MemoryFilesystemDriver, memory_filesystem_registration
from af.framework.errors import PlatformError, PlatformReason
from af.framework.managers import CapabilityBinding, ConnectionRef

_NOW = datetime(2026, 8, 1, 12, tzinfo=UTC)


class BrokenBinaryStream(BufferedIOBase):
    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> str:  # type: ignore[override]
        del size
        return "not-bytes"


def _binding(version: str = "1") -> CapabilityBinding:
    return CapabilityBinding(
        driver_version=version,
        artifact_digest="a" * 64,
        dependency_graph_digest="b" * 64,
        configuration_digest="c" * 64,
        deployment_profile="test",
        policy_version="policy-v1",
    )


def test_memory_filesystem_driver_validates_clocks_streams_and_unsupported_operations() -> None:
    key = FilesystemKey("docs/report.txt")
    upload = MultipartUpload(key=key, upload_id="upload-1")

    with pytest.raises(TypeError):
        MemoryFilesystemDriver(clock="bad")  # type: ignore[arg-type]

    driver = MemoryFilesystemDriver(clock=lambda: datetime(2026, 8, 1, 12))
    with pytest.raises(PlatformError) as invalid_clock:
        driver.write(key, BytesIO(b"content"))
    assert invalid_clock.value.reason is PlatformReason.PERMANENT_FAILURE

    driver = MemoryFilesystemDriver(clock=lambda: _NOW)
    with pytest.raises(FilesystemOperationError):
        driver.read(key)
    with pytest.raises(ValueError):
        driver.write(key, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        driver.write(key, BrokenBinaryStream())
    with pytest.raises(UnsupportedFilesystemFeature) as visibility_error:
        driver.write(key, BytesIO(b"content"), visibility=FileVisibility.PUBLIC)
    assert visibility_error.value.feature == "visibility"

    for operation in (
        lambda: driver.signed_url(key, expires_at=_NOW),
        lambda: driver.set_visibility(key, FileVisibility.PUBLIC),
        lambda: driver.create_multipart_upload(key),
        lambda: driver.upload_part(upload, 1, BytesIO(b"x")),
        lambda: driver.complete_multipart_upload(upload, (MultipartPart(1, "a" * 64),)),
    ):
        with pytest.raises(UnsupportedFilesystemFeature):
            operation()

    driver.close()
    driver.close()
    with pytest.raises(PlatformError) as closed:
        driver.list()
    assert closed.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_memory_filesystem_registration_validates_configuration_and_evidence_clock() -> None:
    registration = memory_filesystem_registration(clock=lambda: _NOW)
    ref = ConnectionRef("filesystem", "primary", "memory")

    with pytest.raises(ValueError):
        registration.factory(ref, {"driver": "memory", "extra": True})
    with pytest.raises(ValueError):
        registration.verifier(ref, object(), _binding())  # type: ignore[arg-type]

    invalid_clock_registration = memory_filesystem_registration(
        clock=lambda: datetime(2026, 8, 1, 12)
    )
    driver = MemoryFilesystemDriver(clock=lambda: _NOW)
    with pytest.raises(ValueError):
        invalid_clock_registration.verifier(ref, driver, _binding())


def test_local_filesystem_driver_covers_listing_metadata_copy_move_delete_and_shutdown(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        LocalFilesystemDriver("")
    with pytest.raises(TypeError):
        LocalFilesystemDriver(tmp_path / "storage", clock="bad")  # type: ignore[arg-type]

    key = FilesystemKey("docs/report.txt")
    root = tmp_path / "storage"
    driver = LocalFilesystemDriver(root, clock=lambda: _NOW)
    driver.write(key, BytesIO(b"content"))
    driver.write(FilesystemKey("docs/other.txt"), BytesIO(b"other"))
    driver.write(FilesystemKey("images/logo.bin"), BytesIO(b"logo"))
    (root / "docs" / "symlink.bin").symlink_to(root / "images" / "logo.bin")

    entries = driver.list(FilesystemKey("docs"))
    assert [entry.key.value for entry in entries] == ["docs/other.txt", "docs/report.txt"]
    assert driver.metadata(key).checksum_sha256 == hashlib.sha256(b"content").hexdigest()
    assert driver.checksum(key) == hashlib.sha256(b"content").hexdigest()

    copy_key = FilesystemKey("copies/report.txt")
    moved_key = FilesystemKey("archive/report.txt")
    driver.copy(key, copy_key)
    driver.move(copy_key, moved_key)

    with driver.read(moved_key) as stream:
        assert stream.read() == b"content"
    assert driver.delete(moved_key)
    assert not driver.delete(moved_key)

    for operation in (
        lambda: driver.metadata(FilesystemKey("missing.txt")),
        lambda: driver.read(FilesystemKey("missing.txt")),
    ):
        with pytest.raises(FilesystemOperationError):
            operation()

    with pytest.raises(ValueError):
        driver.write(key, object())  # type: ignore[arg-type]
    with pytest.raises(FilesystemOperationError) as broken_stream:
        driver.write(FilesystemKey("docs/broken.txt"), BrokenBinaryStream())
    assert broken_stream.value.reason is PlatformReason.PERMANENT_FAILURE

    upload = MultipartUpload(key=key, upload_id="upload-1")
    for operation in (
        lambda: driver.signed_url(key, expires_at=_NOW),
        lambda: driver.set_visibility(key, FileVisibility.PUBLIC),
        lambda: driver.create_multipart_upload(key),
        lambda: driver.upload_part(upload, 1, BytesIO(b"x")),
        lambda: driver.complete_multipart_upload(upload, (MultipartPart(1, "a" * 64),)),
    ):
        with pytest.raises(UnsupportedFilesystemFeature):
            operation()

    driver.close()
    with pytest.raises(FilesystemOperationError) as closed:
        driver.list()
    assert closed.value.reason is PlatformReason.SHUTDOWN_FAILED


def test_local_filesystem_driver_reports_invalid_clock_and_registration_validation(
    tmp_path: Path,
) -> None:
    driver = LocalFilesystemDriver(tmp_path / "storage", clock=lambda: datetime(2026, 8, 1, 12))
    with pytest.raises(FilesystemOperationError) as invalid_clock:
        driver._now()
    assert invalid_clock.value.reason is PlatformReason.PERMANENT_FAILURE

    registration = local_filesystem_registration(clock=lambda: _NOW)
    ref = ConnectionRef("filesystem", "primary", "local")
    with pytest.raises(ValueError):
        registration.factory(ref, {"driver": "local"})
    with pytest.raises(ValueError):
        registration.verifier(ref, object(), _binding())  # type: ignore[arg-type]

    invalid_clock_registration = local_filesystem_registration(
        clock=lambda: datetime(2026, 8, 1, 12)
    )
    valid_driver = LocalFilesystemDriver(tmp_path / "valid", clock=lambda: _NOW)
    with pytest.raises(ValueError):
        invalid_clock_registration.verifier(ref, valid_driver, _binding())
