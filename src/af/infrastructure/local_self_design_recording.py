"""Repository-local recorder for exact, explicitly authorized self-design plans."""

from __future__ import annotations

import hmac
import importlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from af.framework.self_design_recording import (
    SELF_DESIGN_RECORD_MAX_BYTES,
    DesignPlanRecord,
    DesignPlanRecorder,
    DesignRecordingError,
    DesignRecordingRequest,
    DesignRecordResult,
    DesignRecordStatus,
)
from af.framework.version_control import MANIFEST_MAX_BYTES


class LocalDesignPlanRecorder(DesignPlanRecorder):
    """Write one canonical record beneath manifest-owned AF local state."""

    def record(self, request: DesignRecordingRequest) -> DesignRecordResult:
        if not isinstance(request, DesignRecordingRequest):
            raise TypeError("self-design recording request is invalid")
        record = DesignPlanRecord.from_request(request)
        content = record.canonical_bytes()
        if not content or len(content) > SELF_DESIGN_RECORD_MAX_BYTES:
            raise DesignRecordingError("self-design record is too large")

        common_dir = self._common_directory(request)
        with self._repository_lock(common_dir):
            self._validate_manifest(request)
            record_root = common_dir / "af" / "self-design" / "records"
            self._ensure_record_root(common_dir, record_root)
            record_name = request.prepared.plan.digest.removeprefix("sha256:") + ".json"
            record_path = record_root / record_name
            previous = self._read_existing(record_path)
            if previous is not None:
                existing = DesignPlanRecord.from_canonical_bytes(previous, request.prepared)
                if (
                    existing.prepared.plan.digest != record.prepared.plan.digest
                    or existing.prepared.snapshot.digest != record.prepared.snapshot.digest
                ):
                    raise DesignRecordingError("self-design record conflicts with existing state")
                return DesignRecordResult(
                    status=DesignRecordStatus.UNCHANGED,
                    record=existing,
                    record_path=str(record_path),
                )
            self._write_new(record_root, record_path, content)
            return DesignRecordResult(
                status=DesignRecordStatus.RECORDED,
                record=record,
                record_path=str(record_path),
            )

    @staticmethod
    def _common_directory(request: DesignRecordingRequest) -> Path:
        candidate = Path(request.coordination.manifest.repository.git_common_dir)
        try:
            common_dir = candidate.resolve(strict=True)
        except OSError:
            raise DesignRecordingError("self-design coordination state is unavailable") from None
        if not common_dir.is_dir() or candidate.is_symlink():
            raise DesignRecordingError("self-design coordination state is invalid")
        return common_dir

    @staticmethod
    def _validate_manifest(request: DesignRecordingRequest) -> None:
        manifest_path = Path(request.coordination.manifest_path)
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise DesignRecordingError("self-design coordination manifest is invalid")
        try:
            if manifest_path.stat().st_size > MANIFEST_MAX_BYTES:
                raise OSError
            content = manifest_path.read_bytes()
        except OSError:
            raise DesignRecordingError("self-design coordination manifest is unavailable") from None
        expected = request.coordination.manifest.file_bytes()
        if not hmac.compare_digest(content, expected):
            raise DesignRecordingError("self-design coordination manifest is invalid")

    @staticmethod
    def _ensure_record_root(common_dir: Path, record_root: Path) -> None:
        af_dir = common_dir / "af"
        if af_dir.is_symlink() or not af_dir.is_dir():
            raise DesignRecordingError("self-design managed state is invalid")
        for directory in (record_root.parent, record_root):
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise DesignRecordingError("self-design managed state is invalid")
            try:
                directory.mkdir(mode=0o700, exist_ok=True)
                resolved = directory.resolve(strict=True)
            except OSError:
                raise DesignRecordingError("self-design managed state is unavailable") from None
            if not resolved.is_relative_to(common_dir):
                raise DesignRecordingError("self-design managed state is invalid")

    @staticmethod
    def _read_existing(path: Path) -> bytes | None:
        if path.is_symlink():
            raise DesignRecordingError("self-design record state is invalid")
        if not path.exists():
            return None
        if not path.is_file():
            raise DesignRecordingError("self-design record state is invalid")
        try:
            if path.stat().st_size > SELF_DESIGN_RECORD_MAX_BYTES:
                raise OSError
            return path.read_bytes()
        except OSError:
            raise DesignRecordingError("self-design record state is invalid") from None

    @staticmethod
    def _write_new(record_root: Path, record_path: Path, content: bytes) -> None:
        descriptor = -1
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".record-", dir=record_root)
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary_path, record_path)
            if os.name != "nt":
                directory_descriptor = os.open(record_root, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        except FileExistsError:
            raise DesignRecordingError("self-design record state changed concurrently") from None
        except OSError:
            raise DesignRecordingError("self-design record could not be written") from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path is not None:
                with suppress(OSError):
                    temporary_path.unlink(missing_ok=True)

    @staticmethod
    @contextmanager
    def _repository_lock(common_dir: Path) -> Iterator[None]:
        lock_path = common_dir / "af-self-design-record.lock"
        if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
            raise DesignRecordingError("self-design recording lock is invalid")
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
            os.fchmod(descriptor, 0o600)
        except OSError:
            raise DesignRecordingError("self-design recording lock is unavailable") from None
        windows_lock = os.name == "nt"
        try:
            if windows_lock:
                msvcrt = importlib.import_module("msvcrt")
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError:
            os.close(descriptor)
            raise DesignRecordingError("self-design recording lock failed") from None
        try:
            yield
        finally:
            try:
                if windows_lock:
                    msvcrt = importlib.import_module("msvcrt")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                os.close(descriptor)
