"""Private SQLite snapshot backup, new-file restore and bounded core retention.

Restores never overwrite a running database. Backups are independently retained;
privacy deletion from the live database does not remove previous backups.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult

from af.framework.data_lifecycle import BackupReceipt, RetentionResult, SchemaStatus
from af.infrastructure.database import (
    Database,
    JobEventRow,
    ProgramJobExecutionRow,
    ProgramJobResultRow,
    TaskRow,
    TraceRow,
)

_MAX_BACKUP_BYTES = 1_073_741_824
_TERMINAL = frozenset({"completed", "failed", "cancelled", "budget_exhausted"})


def _checksum(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _unlink_owned(path: Path, owned: os.stat_result) -> None:
    with suppress(OSError):
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
            path.unlink()


def _private_write(path: Path, payload: bytes) -> os.stat_result:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    owned = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
            raise OSError("private data destination changed")
        return owned
    except BaseException:
        _unlink_owned(path, owned)
        raise


def _integrity(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite backup integrity validation failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite backup foreign-key validation failed")


class SQLiteDataMaintenance:
    def __init__(self, database: Database, *, authentication_key: bytes) -> None:
        if database.engine.url.get_backend_name() != "sqlite":
            raise ValueError("reference data maintenance requires SQLite")
        if not isinstance(authentication_key, bytes) or len(authentication_key) < 32:
            raise ValueError("backup authentication key must be at least 32 bytes")
        self._database = database
        self._key = authentication_key

    def initialize(self) -> SchemaStatus:
        from af.infrastructure.reference_schema import reference_migrations

        self._database.verify_schema()
        migrations = reference_migrations()
        fingerprint = hashlib.sha256(
            ":".join(migration.checksum for migration in migrations).encode()
        ).hexdigest()
        return SchemaStatus(tuple(migration.migration_id for migration in migrations), fingerprint)

    def _signature(self, data: dict[str, object]) -> str:
        payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self._key, payload, hashlib.sha256).hexdigest()

    def backup(self, destination: Path) -> BackupReceipt:
        destination = destination.absolute()
        manifest_path = destination.with_name(destination.name + ".manifest.json")
        if destination.exists() or destination.is_symlink() or manifest_path.exists():
            raise FileExistsError("backup destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".af-backup-", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        owned = temporary.stat(follow_symlinks=False)
        published = False
        try:
            raw = self._database.engine.raw_connection()
            try:
                connection = raw.driver_connection
                if not isinstance(connection, sqlite3.Connection):
                    raise TypeError("backup requires a SQLite DBAPI connection")
                with sqlite3.connect(temporary) as target:
                    connection.backup(target, pages=256)
                    _integrity(target)
            finally:
                raw.close()
            size = temporary.stat().st_size
            if size > _MAX_BACKUP_BYTES:
                raise ValueError("backup exceeds reference size bound")
            digest = _checksum(temporary)
            created = datetime.now(UTC)
            metadata: dict[str, object] = {
                "format_version": 1,
                "sha256": digest,
                "size_bytes": size,
                "created_at": created.isoformat(),
                "retention_scope": "separate-backup",
                "key_retention": "retain the source workflow key separately",
            }
            metadata["signature"] = self._signature(metadata)
            with temporary.open("rb") as source:
                os.fsync(source.fileno())
            # Hard-link publication is atomic and refuses a concurrently-created destination.
            os.link(temporary, destination)
            published = True
            _private_write(manifest_path, json.dumps(metadata, sort_keys=True).encode())
            return BackupReceipt(destination, manifest_path, digest, size, created)
        except BaseException:
            if published:
                _unlink_owned(destination, owned)
            raise
        finally:
            temporary.unlink(missing_ok=True)

    def restore(self, backup: Path, destination: Path) -> Path:
        backup = backup.absolute()
        destination = destination.absolute()
        key_path = destination.with_suffix(".workflow-key")
        if key_path == destination:
            raise ValueError("restore database destination conflicts with its key sidecar")
        if (
            destination.exists()
            or destination.is_symlink()
            or key_path.exists()
            or key_path.is_symlink()
        ):
            raise FileExistsError("restore destination or key already exists")
        if backup.is_symlink() or not backup.is_file():
            raise ValueError("backup must be a regular local file")
        manifest = backup.with_name(backup.name + ".manifest.json")
        if manifest.is_symlink() or manifest.stat().st_size > 4096:
            raise ValueError("backup manifest is invalid")
        loaded: object = json.loads(manifest.read_bytes())
        if not isinstance(loaded, dict):
            raise ValueError("backup manifest is invalid")
        metadata = cast(dict[str, object], loaded)
        signature = metadata.pop("signature", None)
        if not isinstance(signature, str) or not hmac.compare_digest(
            signature, self._signature(metadata)
        ):
            raise ValueError("backup manifest authentication failed")
        if metadata.get("format_version") != 1:
            raise ValueError("backup version is incompatible")
        if (
            backup.stat().st_size > _MAX_BACKUP_BYTES
            or metadata.get("size_bytes") != backup.stat().st_size
            or metadata.get("sha256") != _checksum(backup)
        ):
            raise ValueError("backup checksum mismatch")
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".af-restore-", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            # Read-only URI prevents accidental source mutation during validation.
            with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as source:
                _integrity(source)
                with sqlite3.connect(temporary) as target:
                    source.backup(target, pages=256)
                    _integrity(target)
            with temporary.open("rb") as restored:
                os.fsync(restored.fileno())
            # Verification used the original host key; retain that identity at
            # the restored database's bootstrap location before publishing it.
            key_owned = _private_write(key_path, self._key)
            try:
                os.link(temporary, destination)
            except BaseException:
                _unlink_owned(key_path, key_owned)
                raise
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    def retain(self, *, before: datetime, dry_run: bool = True) -> RetentionResult:
        """Remove expired core records; preserve active jobs and referenced traces.

        Workflow, admission, auth, and migration histories are deliberately outside
        this core retention policy and require their separate governed policies.
        """
        if before.utcoffset() is None:
            raise ValueError("retention cutoff must be timezone aware")
        if type(dry_run) is not bool:
            raise ValueError("retention dry_run must be boolean")
        cutoff = before.astimezone(UTC)
        with self._database.sessions.begin() as session:
            task_ids = select(TaskRow.id).where(TaskRow.updated_at < cutoff)
            trace_ids = select(TraceRow.id).where(
                TraceRow.created_at < cutoff,
                ~TraceRow.id.in_(select(TaskRow.trace_id).where(TaskRow.updated_at >= cutoff)),
            )
            last_events = (
                select(JobEventRow.job_id, func.max(JobEventRow.sequence).label("sequence"))
                .group_by(JobEventRow.job_id)
                .subquery()
            )
            expired_jobs = (
                select(JobEventRow.job_id)
                .join(
                    last_events,
                    (JobEventRow.job_id == last_events.c.job_id)
                    & (JobEventRow.sequence == last_events.c.sequence),
                )
                .where(JobEventRow.created_at < cutoff, JobEventRow.state.in_(_TERMINAL))
            )
            predicates = (
                (TaskRow, TaskRow.id.in_(task_ids)),
                (TraceRow, TraceRow.id.in_(trace_ids)),
                (ProgramJobResultRow, ProgramJobResultRow.job_id.in_(expired_jobs)),
                (ProgramJobExecutionRow, ProgramJobExecutionRow.job_id.in_(expired_jobs)),
                (JobEventRow, JobEventRow.job_id.in_(expired_jobs)),
            )
            counts = []
            for model, predicate in predicates:
                if dry_run:
                    count = session.scalar(select(func.count()).select_from(model).where(predicate))
                    counts.append((model.__tablename__, count or 0))
                else:
                    result = session.execute(delete(model).where(predicate))
                    counts.append(
                        (
                            model.__tablename__,
                            cast(CursorResult[tuple[object, ...]], result).rowcount,
                        )
                    )
        return RetentionResult(cutoff, dry_run, tuple(counts))
