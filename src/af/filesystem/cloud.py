"""S3-compatible, Azure Blob, and GCS adapters with caller-owned SDK clients.

Importing this module installs no SDK and opens no connection. Pass configured
clients explicitly, including credentials, retry and network policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import suppress
from datetime import datetime
from typing import Any, BinaryIO, Protocol

from af.filesystem.contracts import ByteRange
from af.filesystem.object_store import ObjectInfo, ObjectStorageFilesystemDriver


class S3Client(Protocol):
    def head_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def head_bucket(self, **kwargs: Any) -> object: ...
    def get_object(self, **kwargs: Any) -> dict[str, Any]: ...
    def put_object(self, **kwargs: Any) -> object: ...
    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]: ...
    def delete_object(self, **kwargs: Any) -> object: ...
    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]: ...
    def upload_part(self, **kwargs: Any) -> dict[str, Any]: ...
    def complete_multipart_upload(self, **kwargs: Any) -> object: ...
    def abort_multipart_upload(self, **kwargs: Any) -> object: ...


def _missing(error: Exception) -> bool:
    if isinstance(error, FileNotFoundError):
        return True
    if getattr(error, "status_code", None) == 404 or getattr(error, "code", None) == 404:
        return True
    response = getattr(error, "response", None)
    return isinstance(response, dict) and response.get("Error", {}).get("Code") in {
        "404",
        "NoSuchKey",
        "NotFound",
    }


class _S3Backend:
    def __init__(self, client: S3Client, bucket: str) -> None:
        if not bucket:
            raise ValueError("S3 bucket is required")
        self.client, self.bucket = client, bucket

    def check(self) -> None:
        self.client.head_bucket(Bucket=self.bucket)

    def stat(self, key: str) -> ObjectInfo:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
            return ObjectInfo(
                response["ContentLength"], response["LastModified"], response.get("VersionId")
            )
        except Exception as error:
            if _missing(error):
                raise FileNotFoundError(key) from None
            raise

    def chunks(self, key: str, byte_range: ByteRange | None = None) -> Iterator[bytes]:
        options: dict[str, object] = {}
        if byte_range is not None:
            end = "" if byte_range.end is None else str(byte_range.end)
            options["Range"] = f"bytes={byte_range.start}-{end}"
        response = self.client.get_object(Bucket=self.bucket, Key=key, **options)
        body = response["Body"]
        try:
            while data := body.read(64 * 1024):
                yield data
        finally:
            body.close()

    def put(self, key: str, chunks: Iterator[bytes]) -> None:
        first = next(chunks, None)
        if first is None:
            self.client.put_object(Bucket=self.bucket, Key=key, Body=b"")
            return
        upload_id = self.client.create_multipart_upload(Bucket=self.bucket, Key=key)["UploadId"]
        parts: list[dict[str, object]] = []
        buffer = bytearray()

        def upload(data: bytes) -> None:
            if len(parts) >= 10_000:
                raise ValueError("S3 multipart part limit exceeded")
            number = len(parts) + 1
            result = self.client.upload_part(
                Bucket=self.bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=data
            )
            parts.append({"PartNumber": number, "ETag": result["ETag"]})

        try:
            import itertools

            for chunk in itertools.chain((first,), chunks):
                buffer.extend(chunk)
                while len(buffer) >= 8 * 1024 * 1024:
                    upload(bytes(buffer[: 8 * 1024 * 1024]))
                    del buffer[: 8 * 1024 * 1024]
            if buffer:
                upload(bytes(buffer))
            self.client.complete_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts}
            )
        except BaseException:
            with suppress(Exception):
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
            raise

    def list(self, prefix: str) -> Iterable[tuple[str, ObjectInfo]]:
        token: str | None = None
        while True:
            options = {} if token is None else {"ContinuationToken": token}
            page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix, **options)
            for entry in page.get("Contents", ()):
                yield entry["Key"], ObjectInfo(entry["Size"], entry["LastModified"])
            if not page.get("IsTruncated"):
                break
            next_token = page.get("NextContinuationToken")
            if not isinstance(next_token, str) or next_token == token:
                raise ValueError("S3 returned an invalid pagination cursor")
            token = next_token

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


class S3FilesystemDriver(ObjectStorageFilesystemDriver):
    """S3/S3-compatible private disk, using streaming multipart internally.

    Caller configures bucket policy. No per-object ACL, atomic rename, public
    visibility, signed URL or explicit multipart management is promised.
    """

    def __init__(self, client: S3Client, bucket: str) -> None:
        super().__init__(_S3Backend(client, bucket))


class AzureBlobProperties(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def size(self) -> int: ...
    @property
    def last_modified(self) -> datetime: ...
    @property
    def version_id(self) -> str | None: ...


class AzureDownloader(Protocol):
    def chunks(self) -> Iterator[bytes]: ...


class AzureBlob(Protocol):
    def get_blob_properties(self) -> AzureBlobProperties: ...
    def download_blob(self, **kwargs: Any) -> AzureDownloader: ...
    def upload_blob(self, data: Iterable[bytes], *, overwrite: bool) -> object: ...
    def delete_blob(self) -> object: ...


class AzureContainer(Protocol):
    def get_blob_client(self, blob: str) -> AzureBlob: ...
    def list_blobs(self, *, name_starts_with: str) -> Iterable[AzureBlobProperties]: ...
    def get_container_properties(self) -> object: ...


class _AzureBackend:
    def __init__(self, container: AzureContainer) -> None:
        self.container = container

    def check(self) -> None:
        self.container.get_container_properties()

    def stat(self, key: str) -> ObjectInfo:
        try:
            result = self.container.get_blob_client(key).get_blob_properties()
            return ObjectInfo(result.size, result.last_modified, result.version_id)
        except Exception as error:
            if _missing(error):
                raise FileNotFoundError(key) from None
            raise

    def chunks(self, key: str, byte_range: ByteRange | None = None) -> Iterator[bytes]:
        options: dict[str, int] = {}
        if byte_range is not None:
            options["offset"] = byte_range.start
            if byte_range.end is not None:
                options["length"] = byte_range.end - byte_range.start + 1
        yield from self.container.get_blob_client(key).download_blob(**options).chunks()

    def put(self, key: str, chunks: Iterator[bytes]) -> None:
        self.container.get_blob_client(key).upload_blob(chunks, overwrite=True)

    def list(self, prefix: str) -> Iterable[tuple[str, ObjectInfo]]:
        for blob in self.container.list_blobs(name_starts_with=prefix):
            yield blob.name, ObjectInfo(blob.size, blob.last_modified, blob.version_id)

    def delete(self, key: str) -> None:
        self.container.get_blob_client(key).delete_blob()


class AzureBlobFilesystemDriver(ObjectStorageFilesystemDriver):
    def __init__(self, container: AzureContainer) -> None:
        super().__init__(_AzureBackend(container))


class GCSBlob(Protocol):
    @property
    def name(self) -> str: ...
    @property
    def size(self) -> int | None: ...
    @property
    def updated(self) -> datetime | None: ...
    @property
    def generation(self) -> int | None: ...
    def reload(self) -> None: ...
    def open(self, mode: str, *, chunk_size: int) -> BinaryIO: ...
    def delete(self) -> None: ...


class GCSBucket(Protocol):
    def blob(self, blob_name: str) -> GCSBlob: ...
    def list_blobs(self, *, prefix: str) -> Iterable[GCSBlob]: ...
    def reload(self) -> None: ...


def _gcs_info(blob: GCSBlob) -> ObjectInfo:
    if blob.size is None or blob.updated is None:
        raise ValueError("GCS object metadata is incomplete")
    return ObjectInfo(
        blob.size, blob.updated, None if blob.generation is None else str(blob.generation)
    )


class _GCSBackend:
    def __init__(self, bucket: GCSBucket) -> None:
        self.bucket = bucket

    def check(self) -> None:
        self.bucket.reload()

    def stat(self, key: str) -> ObjectInfo:
        try:
            blob = self.bucket.blob(key)
            blob.reload()
            return _gcs_info(blob)
        except Exception as error:
            if _missing(error):
                raise FileNotFoundError(key) from None
            raise

    def chunks(self, key: str, byte_range: ByteRange | None = None) -> Iterator[bytes]:
        blob = self.bucket.blob(key)
        blob.reload()  # Pin a generation to avoid mixing versions across chunk requests.
        with blob.open("rb", chunk_size=8 * 1024 * 1024) as stream:
            remaining = None
            if byte_range is not None:
                stream.seek(byte_range.start)
                if byte_range.end is not None:
                    remaining = byte_range.end - byte_range.start + 1
            while remaining is None or remaining > 0:
                data = stream.read(64 * 1024 if remaining is None else min(64 * 1024, remaining))
                if not data:
                    break
                yield data
                if remaining is not None:
                    remaining -= len(data)

    def put(self, key: str, chunks: Iterator[bytes]) -> None:
        with self.bucket.blob(key).open("wb", chunk_size=8 * 1024 * 1024) as stream:
            for chunk in chunks:
                stream.write(chunk)

    def list(self, prefix: str) -> Iterable[tuple[str, ObjectInfo]]:
        for blob in self.bucket.list_blobs(prefix=prefix):
            yield blob.name, _gcs_info(blob)

    def delete(self, key: str) -> None:
        self.bucket.blob(key).delete()


class GCSFilesystemDriver(ObjectStorageFilesystemDriver):
    def __init__(self, bucket: GCSBucket) -> None:
        super().__init__(_GCSBackend(bucket))
