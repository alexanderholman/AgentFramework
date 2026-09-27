from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from af.filesystem.contracts import ByteRange, FilesystemKey, FilesystemOperationError
from af.filesystem.sftp import SFTPFilesystemDriver


class LocalSFTPPort:
    """Local-file-backed port double; not an SFTP service certification."""

    def normalize(self, path):
        return str(Path(path).resolve())

    def lstat(self, path):
        result = Path(path).lstat()
        return SimpleNamespace(
            filename=Path(path).name,
            st_size=result.st_size,
            st_mode=result.st_mode,
            st_mtime=int(result.st_mtime),
        )

    def listdir_attr(self, path):
        return [self.lstat(item) for item in Path(path).iterdir()]

    def open(self, path, mode):
        return open(path, mode)

    def mkdir(self, path, mode):
        Path(path).mkdir(mode=mode)

    def chmod(self, path, mode):
        Path(path).chmod(mode)

    def remove(self, path):
        Path(path).unlink()


def test_sftp_streams_files_and_denies_symlink_namespace_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "secret").write_bytes(b"private")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    driver = SFTPFilesystemDriver(LocalSFTPPort(), str(root))
    key = FilesystemKey("nested/binary")
    data = bytes(range(256)) * 1000
    written = driver.write(key, BytesIO(data))
    assert written.size == len(data)
    assert written.updated_at <= datetime.now(UTC)
    assert (root / "nested" / "binary").stat().st_mode & 0o777 == 0o600
    with driver.read(key, byte_range=ByteRange(10, 99)) as stream:
        assert stream.read() == data[10:100]
    with driver.read(key, byte_range=ByteRange(10)) as stream:
        assert stream.read() == data[10:]
    assert [entry.key for entry in driver.list()] == [key]
    with pytest.raises(FilesystemOperationError):
        driver.read(FilesystemKey("escape/secret"))
    with pytest.raises(FilesystemOperationError):
        driver.write(FilesystemKey("escape/new"), BytesIO(b"bad"))
    with pytest.raises(FilesystemOperationError):
        driver.write(FilesystemKey("nested/binary/child"), BytesIO(b"bad"))
    with pytest.raises(FilesystemOperationError):
        driver.read(FilesystemKey("missing/child"))
    with pytest.raises(FilesystemOperationError):
        driver.metadata(FilesystemKey("nested"))
    assert driver.delete(key)
    assert not driver.delete(key)


def test_sftp_rejects_non_directory_and_relative_roots(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SFTPFilesystemDriver(LocalSFTPPort(), "relative")
    file = tmp_path / "file"
    file.write_bytes(b"x")
    with pytest.raises(ValueError):
        SFTPFilesystemDriver(LocalSFTPPort(), str(file))
