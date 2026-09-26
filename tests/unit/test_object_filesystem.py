from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO

import pytest

from af.filesystem.contracts import (
    ByteRange,
    FilesystemKey,
    FileVisibility,
    UnsupportedFilesystemFeature,
)
from af.filesystem.object_store import ObjectInfo, ObjectStorageFilesystemDriver

NOW = datetime(2026, 9, 26, tzinfo=UTC)


class Backend:
    def __init__(self):
        self.objects = {}
        self.reads = []

    def stat(self, key):
        if key not in self.objects:
            raise FileNotFoundError(key)
        return ObjectInfo(len(self.objects[key]), NOW, "version-1")

    def chunks(self, key, byte_range=None):
        data = self.objects[key]
        if byte_range is not None:
            start, end = byte_range.bounds(len(data))
            data = data[start:end]
        for offset in range(0, len(data), 17):
            self.reads.append(17)
            yield data[offset : offset + 17]

    def put(self, key, chunks):
        self.objects[key] = b"".join(chunks)

    def list(self, prefix):
        return [(key, self.stat(key)) for key in self.objects if key.startswith(prefix)]

    def delete(self, key):
        self.objects.pop(key, None)

    def check(self):
        pass


def test_object_driver_round_trip_ranges_copy_move_and_namespace_listing() -> None:
    backend = Backend()
    driver = ObjectStorageFilesystemDriver(backend)
    key = FilesystemKey("docs/report.bin")
    data = bytes(range(256)) * 1000
    result = driver.write(key, BytesIO(data))
    assert result.checksum_sha256 == hashlib.sha256(data).hexdigest()
    assert result.size == len(data)
    with driver.read(key, byte_range=ByteRange(11, 65)) as stream:
        assert stream.read() == data[11:66]
    with driver.read(key, byte_range=ByteRange(len(data))) as stream:
        assert stream.read() == b""
    driver.write(FilesystemKey("docs-other/file"), BytesIO(b"other"))
    assert [entry.key for entry in driver.list(FilesystemKey("docs"))] == [key]
    driver.copy(key, FilesystemKey("copy"))
    driver.move(FilesystemKey("copy"), FilesystemKey("moved"))
    assert driver.checksum(FilesystemKey("moved")) == result.checksum_sha256
    assert not driver.delete(FilesystemKey("copy"))
    assert driver.delete(FilesystemKey("moved"))
    assert driver.metadata(key).version_id == "version-1"
    with pytest.raises(UnsupportedFilesystemFeature):
        driver.write(key, BytesIO(b"public"), visibility=FileVisibility.PUBLIC)


def test_reads_are_lazy_and_writes_never_request_unbounded_input() -> None:
    class BoundedInput(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 8 * 1024 * 1024
            return super().read(size)

    backend = Backend()
    driver = ObjectStorageFilesystemDriver(backend)
    key = FilesystemKey("payload")
    driver.write(key, BoundedInput(b"x" * 200_000))
    backend.reads.clear()
    stream = driver.read(key)
    assert not backend.reads
    assert stream.read(3) == b"xxx"
    assert len(backend.reads) < 10
    stream.close()


def test_object_registration_exposes_only_working_capabilities_and_signed_url_port() -> None:
    from datetime import timedelta

    from af.filesystem.manager import FilesystemFeature, FilesystemManager
    from af.filesystem.object_store import object_filesystem_registration
    from af.framework.managers import CapabilityValidationError

    backend = Backend()
    manager = FilesystemManager(
        connections={"remote": {"driver": "test-object"}},
        default="remote",
        deployment_profile="test",
        policy_version="v1",
    )
    manager.install(
        object_filesystem_registration(
            "test-object", lambda _: ObjectStorageFilesystemDriver(backend)
        )
    )
    key = FilesystemKey("file")
    manager.write(key, BytesIO(b"data"))
    assert (
        manager.connection(requires=(FilesystemFeature.STREAMING,)).checksum(key)
        == hashlib.sha256(b"data").hexdigest()
    )
    with pytest.raises(CapabilityValidationError):
        manager.signed_url(key, expires_at=datetime.now(UTC) + timedelta(minutes=5))
    manager.close()
    driver = ObjectStorageFilesystemDriver(
        backend, signer=lambda key, expiry, operation: "https://storage.example.test/" + key
    )
    assert "signed-urls" in driver.capabilities
    assert driver.signed_url(key, expires_at=datetime.now(UTC) + timedelta(minutes=5)).url.endswith(
        "/file"
    )
    with pytest.raises(ValueError):
        driver.signed_url(key, expires_at=datetime(2000, 1, 1, tzinfo=UTC))


def test_object_explicitly_rejects_unsupported_features() -> None:
    from datetime import timedelta

    from af.filesystem.contracts import MultipartPart, MultipartUpload

    driver = ObjectStorageFilesystemDriver(Backend())
    key = FilesystemKey("file")
    upload = MultipartUpload(key, "upload")
    part = MultipartPart(1, "a" * 64)
    actions = [
        lambda: driver.signed_url(key, expires_at=datetime.now(UTC) + timedelta(minutes=1)),
        lambda: driver.set_visibility(key, FileVisibility.PUBLIC),
        lambda: driver.create_multipart_upload(key),
        lambda: driver.upload_part(upload, 1, BytesIO(b"x")),
        lambda: driver.complete_multipart_upload(upload, (part,)),
    ]
    for action in actions:
        with pytest.raises(UnsupportedFilesystemFeature):
            action()


def test_object_failures_are_sanitized_and_incomplete_upload_is_detected() -> None:
    from af.filesystem.contracts import FilesystemOperationError

    class Incomplete(Backend):
        def put(self, key, chunks):
            self.objects[key] = b""

    driver = ObjectStorageFilesystemDriver(Incomplete())
    with pytest.raises(FilesystemOperationError):
        driver.write(FilesystemKey("file"), BytesIO(b"data"))

    class Corrupt(Backend):
        def put(self, key, chunks):
            self.objects[key] = b"".join(chunks) + b"corrupt"

    with pytest.raises(FilesystemOperationError):
        ObjectStorageFilesystemDriver(Corrupt()).write(FilesystemKey("file"), BytesIO(b"data"))

    class Broken(Backend):
        def check(self):
            raise RuntimeError("provider secret")

        def chunks(self, key, byte_range=None):
            raise RuntimeError("provider secret")
            yield b""

        def delete(self, key):
            raise RuntimeError("provider secret")

    backend = Broken()
    backend.objects["file"] = b"data"
    driver = ObjectStorageFilesystemDriver(backend)
    for action in (
        driver.check,
        lambda: driver.checksum(FilesystemKey("file")),
        lambda: driver.delete(FilesystemKey("file")),
    ):
        with pytest.raises(FilesystemOperationError) as error:
            action()
        assert "secret" not in str(error.value)


def test_object_listing_rejects_ambiguous_keys_and_ignores_directory_markers() -> None:
    from af.filesystem.contracts import FilesystemOperationError

    backend = Backend()
    backend.objects.update({"docs/": b"", "docs/file": b"x"})
    driver = ObjectStorageFilesystemDriver(backend)
    assert [item.key.value for item in driver.list()] == ["docs/file"]
    backend.objects["docs//ambiguous"] = b"x"
    with pytest.raises(FilesystemOperationError):
        driver.list()
    del backend.objects["docs//ambiguous"]
    driver.copy(FilesystemKey("docs/file"), FilesystemKey("docs/file"))
    driver.move(FilesystemKey("docs/file"), FilesystemKey("docs/file"))
    assert backend.objects["docs/file"] == b"x"
