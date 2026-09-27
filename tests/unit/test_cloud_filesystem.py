"""SDK-port contract tests: these are deliberately not cloud-service evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from af.filesystem.cloud import AzureBlobFilesystemDriver, GCSFilesystemDriver, S3FilesystemDriver
from af.filesystem.contracts import ByteRange, FilesystemKey, FilesystemOperationError

NOW = datetime(2026, 9, 26, tzinfo=UTC)


@pytest.mark.parametrize("vendor", ["s3", "azure", "gcs"])
def test_cloud_ports_stream_binary_integrity_and_ranges(vendor: str) -> None:
    data = bytes(range(256)) * 1100
    objects = {}
    client = MagicMock()
    if vendor == "s3":
        parts = {}

        def stat(**kwargs):
            key = kwargs["Key"]
            if key not in objects:
                raise FileNotFoundError(key)
            return {"ContentLength": len(objects[key]), "LastModified": NOW, "VersionId": "1"}

        def read(**kwargs):
            value = objects[kwargs["Key"]]
            if "Range" in kwargs:
                start, end = kwargs["Range"].removeprefix("bytes=").split("-")
                value = value[int(start) : int(end) + 1 if end else None]
            return {"Body": BytesIO(value)}

        def upload(**kwargs):
            parts[kwargs["PartNumber"]] = kwargs["Body"]
            return {"ETag": str(kwargs["PartNumber"])}

        def complete(**kwargs):
            objects[kwargs["Key"]] = b"".join(
                parts[item["PartNumber"]] for item in kwargs["MultipartUpload"]["Parts"]
            )
            parts.clear()

        client.head_object.side_effect = stat
        client.get_object.side_effect = read
        client.create_multipart_upload.return_value = {"UploadId": "upload"}
        client.upload_part.side_effect = upload
        client.complete_multipart_upload.side_effect = complete
        client.put_object.side_effect = lambda **kwargs: objects.update(
            {kwargs["Key"]: kwargs["Body"]}
        )
        client.list_objects_v2.side_effect = lambda **kwargs: {
            "Contents": [
                {"Key": key, "Size": len(value), "LastModified": NOW}
                for key, value in objects.items()
            ],
            "IsTruncated": False,
        }
        client.delete_object.side_effect = lambda **kwargs: objects.pop(kwargs["Key"], None)
        driver = S3FilesystemDriver(client, "test-bucket")
    elif vendor == "azure":

        def blob(key):
            result = MagicMock()

            def properties():
                if key not in objects:
                    raise FileNotFoundError(key)
                return SimpleNamespace(
                    name=key, size=len(objects[key]), last_modified=NOW, version_id="1"
                )

            result.get_blob_properties.side_effect = properties
            result.upload_blob.side_effect = lambda chunks, **_: objects.update(
                {key: b"".join(chunks)}
            )

            def download(**kwargs):
                start = kwargs.get("offset", 0)
                end = start + kwargs["length"] if "length" in kwargs else None
                return SimpleNamespace(chunks=lambda: iter([objects[key][start:end]]))

            result.download_blob.side_effect = download
            result.delete_blob.side_effect = lambda: objects.pop(key, None)
            return result

        client.get_blob_client.side_effect = blob
        client.list_blobs.side_effect = lambda **_: [
            blob(key).get_blob_properties() for key in objects
        ]
        driver = AzureBlobFilesystemDriver(client)
    else:

        class Writer(BytesIO):
            def __init__(self, key):
                super().__init__()
                self.key = key

            def close(self):
                if not self.closed:
                    objects[self.key] = self.getvalue()
                super().close()

        def blob(key):
            result = MagicMock()
            result.name = key
            result.updated = NOW
            result.generation = 1

            def reload():
                if key not in objects:
                    raise FileNotFoundError(key)
                result.size = len(objects[key])

            result.reload.side_effect = reload
            result.open.side_effect = lambda mode, **_: (
                Writer(key) if mode == "wb" else BytesIO(objects[key])
            )
            result.delete.side_effect = lambda: objects.pop(key, None)
            if key in objects:
                result.size = len(objects[key])
            return result

        client.blob.side_effect = blob
        client.list_blobs.side_effect = lambda **_: [blob(key) for key in objects]
        driver = GCSFilesystemDriver(client)
    driver.check()
    key = FilesystemKey("binary.bin")
    written = driver.write(key, BytesIO(data))
    assert written.size == len(data)
    assert driver.metadata(key).checksum_sha256 == written.checksum_sha256
    with driver.read(key, byte_range=ByteRange(10, 59)) as stream:
        assert stream.read() == data[10:60]
    with driver.read(key, byte_range=ByteRange(100)) as stream:
        assert stream.read() == data[100:]
    assert driver.list()[0].key == key
    driver.write(FilesystemKey("empty"), BytesIO())
    assert driver.metadata(FilesystemKey("empty")).size == 0
    assert driver.delete(key)
    assert not driver.delete(key)
    driver.close()
    with pytest.raises(FilesystemOperationError):
        driver.read(key)


def test_s3_aborts_failed_upload_and_does_not_report_completion() -> None:
    client = MagicMock()
    client.create_multipart_upload.return_value = {"UploadId": "upload"}
    client.upload_part.side_effect = OSError("connection lost")
    driver = S3FilesystemDriver(client, "bucket")
    with pytest.raises(FilesystemOperationError):
        driver.write(FilesystemKey("binary"), BytesIO(b"data"))
    client.abort_multipart_upload.assert_called_once()
    client.complete_multipart_upload.assert_not_called()


def test_s3_paginates_and_rejects_repeating_cursor() -> None:
    client = MagicMock()
    client.list_objects_v2.side_effect = [
        {
            "Contents": [{"Key": "a", "Size": 1, "LastModified": NOW}],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        },
        {"Contents": [{"Key": "b", "Size": 2, "LastModified": NOW}], "IsTruncated": False},
    ]
    driver = S3FilesystemDriver(client, "bucket")
    assert [entry.key.value for entry in driver.list()] == ["a", "b"]
    assert client.list_objects_v2.call_args.kwargs["ContinuationToken"] == "next"
    client.list_objects_v2.side_effect = None
    client.list_objects_v2.return_value = {"IsTruncated": True, "NextContinuationToken": "same"}
    with pytest.raises(FilesystemOperationError):
        driver.list()
