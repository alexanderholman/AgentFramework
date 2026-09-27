"""Streaming SFTP disk through an explicit authenticated, caller-owned client.

The remote account must control its root exclusively. SFTP has no portable
openat/no-follow primitive, so adversarial concurrent remote path replacement
is outside this adapter's guarantee; discovered symlinks are always rejected.
"""

from __future__ import annotations

import posixpath
import stat
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import BinaryIO, Protocol

from af.filesystem.contracts import ByteRange
from af.filesystem.object_store import ObjectInfo, ObjectStorageFilesystemDriver


class SFTPAttributes(Protocol):
    @property
    def filename(self) -> str: ...
    @property
    def st_size(self) -> int: ...
    @property
    def st_mtime(self) -> int: ...
    @property
    def st_mode(self) -> int: ...


class SFTPClient(Protocol):
    def normalize(self, path: str) -> str: ...
    def lstat(self, path: str) -> SFTPAttributes: ...
    def listdir_attr(self, path: str) -> list[SFTPAttributes]: ...
    def open(self, filename: str, mode: str) -> BinaryIO: ...
    def mkdir(self, path: str, mode: int = 511) -> None: ...
    def chmod(self, path: str, mode: int) -> None: ...
    def remove(self, path: str) -> None: ...


class _SFTPBackend:
    def __init__(self, client: SFTPClient, root: str) -> None:
        if not root.startswith("/"):
            raise ValueError("SFTP root must be absolute")
        self.client = client
        self.root = client.normalize(root).rstrip("/") or "/"
        self.check()

    def check(self) -> None:
        mode = self.client.lstat(self.root).st_mode
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise ValueError("SFTP root must be a directory")

    def _path(self, key: str, *, create_parents: bool = False) -> str:
        current = self.root
        parts = key.split("/")
        for index, part in enumerate(parts):
            if not part or part in {".", ".."}:
                raise ValueError("Invalid SFTP logical key")
            current = posixpath.join(current, part)
            try:
                mode = self.client.lstat(current).st_mode
            except FileNotFoundError:
                if create_parents and index < len(parts) - 1:
                    self.client.mkdir(current, mode=0o700)
                elif index < len(parts) - 1:
                    raise
                continue
            if stat.S_ISLNK(mode):
                raise ValueError("SFTP symlinks are not permitted")
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                raise ValueError("SFTP parent must be a directory")
        return current

    def stat(self, key: str) -> ObjectInfo:
        attrs = self.client.lstat(self._path(key))
        if not stat.S_ISREG(attrs.st_mode):
            raise ValueError("SFTP key must identify a regular file")
        return ObjectInfo(attrs.st_size, datetime.fromtimestamp(attrs.st_mtime, UTC))

    def chunks(self, key: str, byte_range: ByteRange | None = None) -> Iterator[bytes]:
        with self.client.open(self._path(key), "rb") as stream:
            remaining = None
            if byte_range is not None:
                stream.seek(byte_range.start)
                if byte_range.end is not None:
                    remaining = byte_range.end - byte_range.start + 1
            while remaining is None or remaining > 0:
                data = stream.read(64 * 1024 if remaining is None else min(remaining, 64 * 1024))
                if not data:
                    break
                yield data
                if remaining is not None:
                    remaining -= len(data)

    def put(self, key: str, chunks: Iterator[bytes]) -> None:
        path = self._path(key, create_parents=True)
        with self.client.open(path, "wb") as stream:
            self.client.chmod(path, 0o600)
            for chunk in chunks:
                stream.write(chunk)

    def list(self, prefix: str) -> Iterable[tuple[str, ObjectInfo]]:
        pending = [(self.root, "")]
        while pending:
            path, key = pending.pop()
            for entry in self.client.listdir_attr(path):
                if "/" in entry.filename or entry.filename in {".", ".."}:
                    raise ValueError("SFTP returned an invalid directory entry")
                name = posixpath.join(key, entry.filename)
                full = posixpath.join(path, entry.filename)
                if stat.S_ISLNK(entry.st_mode):
                    continue
                if stat.S_ISDIR(entry.st_mode):
                    pending.append((full, name))
                elif stat.S_ISREG(entry.st_mode) and name.startswith(prefix):
                    yield (
                        name,
                        ObjectInfo(entry.st_size, datetime.fromtimestamp(entry.st_mtime, UTC)),
                    )

    def delete(self, key: str) -> None:
        self.client.remove(self._path(key))


class SFTPFilesystemDriver(ObjectStorageFilesystemDriver):
    def __init__(self, client: SFTPClient, root: str) -> None:
        super().__init__(_SFTPBackend(client, root))
