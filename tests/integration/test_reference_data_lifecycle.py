from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from af.infrastructure.data_maintenance import SQLiteDataMaintenance
from af.infrastructure.database import Database


def test_reference_database_has_versioned_schema_and_detects_drift(tmp_path):
    path = tmp_path / "state.db"
    database = Database(f"sqlite:///{path}")
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM af_migrations")).scalar() >= 1
    database.close()
    broken = Database(f"sqlite:///{path}")
    with broken.engine.begin() as connection:
        connection.execute(text("ALTER TABLE af_tasks ADD COLUMN unknown_extra TEXT"))
    broken.close()
    with pytest.raises(Exception, match="integrity"):
        Database(f"sqlite:///{path}")


def test_private_backup_restore_and_checksum_failure(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    maintenance = SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    backup = maintenance.backup(tmp_path / "backup.db")
    assert backup.path.exists()
    assert os.stat(backup.path).st_mode & 0o777 == 0o600
    restored = maintenance.restore(backup.path, tmp_path / "restored.db")
    restored_database = Database(f"sqlite:///{restored}")
    restored_database.close()
    with backup.path.open("ab") as output:
        output.write(b"tampered")
    with pytest.raises(ValueError, match="checksum"):
        maintenance.restore(backup.path, tmp_path / "bad.db")
    assert not (tmp_path / "bad.db").exists()
    database.close()


def test_backup_and_restore_refuse_overwrite(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    maintenance = SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    backup = maintenance.backup(tmp_path / "backup.db")
    with pytest.raises(FileExistsError):
        maintenance.backup(backup.path)
    existing = tmp_path / "existing.db"
    existing.write_bytes(b"untouched")
    with pytest.raises(FileExistsError):
        maintenance.restore(backup.path, existing)
    assert existing.read_bytes() == b"untouched"
    database.close()


def test_retention_preserves_active_jobs_and_live_task_trace_references(tmp_path):
    from datetime import UTC, datetime, timedelta

    from af.infrastructure.database import (
        JobEventRow,
        ProgramJobExecutionRow,
        ProgramJobResultRow,
        TaskRow,
        TraceRow,
    )

    database = Database(f"sqlite:///{tmp_path / 'retention.db'}")
    now = datetime.now(UTC)
    old = now - timedelta(days=10)
    with database.sessions.begin() as session:
        for identifier, updated in [("expired", old), ("retained", now)]:
            session.add(
                TraceRow(
                    id=identifier,
                    prompt="private",
                    intent="test",
                    arguments_json="{}",
                    route="test",
                    tier="static",
                    output="private",
                    verified=True,
                    reused=False,
                    regression_passed=True,
                    promoted=False,
                    status="completed",
                    events_json="[]",
                    created_at=old,
                )
            )
            session.add(
                TaskRow(
                    id=identifier,
                    prompt="private",
                    output="private",
                    intent="test",
                    route="test",
                    tier="static",
                    status="completed",
                    verified=True,
                    reused=False,
                    trace_id=identifier,
                    created_at=old,
                    updated_at=updated,
                )
            )
            session.add(
                JobEventRow(
                    job_id=identifier,
                    sequence=1,
                    state="completed" if identifier == "expired" else "running",
                    stage=None,
                    progress=None,
                    detail=None,
                    created_at=old,
                )
            )
            session.add(ProgramJobExecutionRow(job_id=identifier, claimed_at=old))
            session.add(
                ProgramJobResultRow(
                    job_id=identifier,
                    state="completed",
                    exit_code=0,
                    stdout=b"private",
                    stderr=b"",
                    elapsed_seconds=1,
                    finished_at=old,
                )
            )
    maintenance = SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    before = now - timedelta(days=1)
    preview = maintenance.retain(before=before)
    assert preview.dry_run
    assert all(count == 1 for _name, count in preview.removed)
    with database.sessions() as session:
        assert session.get(TaskRow, "expired") is not None
    result = maintenance.retain(before=before, dry_run=False)
    assert result.removed == preview.removed
    with database.sessions() as session:
        assert session.get(TaskRow, "expired") is None
        assert session.get(TraceRow, "expired") is None
        assert session.get(TaskRow, "retained") is not None
        assert session.get(TraceRow, "retained") is not None
        assert session.get(ProgramJobResultRow, "retained") is not None
    with pytest.raises(ValueError, match="timezone"):
        maintenance.retain(before=datetime.now())
    with pytest.raises(ValueError, match="boolean"):
        maintenance.retain(before=before, dry_run=1)
    database.close()


def rewrite_manifest(path, updates):
    import hashlib
    import hmac
    import json

    manifest = path.with_name(path.name + ".manifest.json")
    data = json.loads(manifest.read_text())
    data.pop("signature")
    data.update(updates)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    data["signature"] = hmac.new(b"k" * 32, payload, hashlib.sha256).hexdigest()
    manifest.write_text(json.dumps(data))


def test_restore_rejects_manifest_tampering_unknown_version_and_invalid_shape(tmp_path):
    import json

    database = Database("sqlite:///:memory:")
    maintenance = SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    backup = maintenance.backup(tmp_path / "backup.db")
    original = backup.manifest_path.read_bytes()
    backup.manifest_path.write_text("[]")
    with pytest.raises(ValueError, match="manifest"):
        maintenance.restore(backup.path, tmp_path / "bad.db")
    backup.manifest_path.write_bytes(original)
    tampered = json.loads(original)
    tampered["size_bytes"] += 1
    backup.manifest_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="authentication"):
        maintenance.restore(backup.path, tmp_path / "bad.db")
    backup.manifest_path.write_bytes(original)
    rewrite_manifest(backup.path, {"format_version": 99})
    with pytest.raises(ValueError, match="version"):
        maintenance.restore(backup.path, tmp_path / "bad.db")
    assert not (tmp_path / "bad.db").exists()
    database.close()


def test_backup_size_bound_and_manifest_publication_failure_cleanup(tmp_path, monkeypatch):
    import af.infrastructure.data_maintenance as module

    database = Database("sqlite:///:memory:")
    maintenance = SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    monkeypatch.setattr(module, "_MAX_BACKUP_BYTES", 1)
    with pytest.raises(ValueError, match="size bound"):
        maintenance.backup(tmp_path / "oversized.db")
    assert not (tmp_path / "oversized.db").exists()
    monkeypatch.setattr(module, "_MAX_BACKUP_BYTES", 1_073_741_824)
    (tmp_path / "backup.db.manifest.json").symlink_to(tmp_path / "missing")
    with pytest.raises(FileExistsError):
        maintenance.backup(tmp_path / "backup.db")
    assert not (tmp_path / "backup.db").exists()
    assert not list(tmp_path.glob(".af-backup-*"))
    database.close()


def test_restore_rejects_symlinks_and_unbounded_manifest(tmp_path):
    database = Database("sqlite:///:memory:")
    maintenance = SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    backup = maintenance.backup(tmp_path / "backup.db")
    alias = tmp_path / "alias.db"
    alias.symlink_to(backup.path)
    with pytest.raises(ValueError, match="regular"):
        maintenance.restore(alias, tmp_path / "bad.db")
    backup.manifest_path.write_bytes(b"x" * 5000)
    with pytest.raises(ValueError, match="manifest"):
        maintenance.restore(backup.path, tmp_path / "bad.db")
    with pytest.raises(ValueError, match="key"):
        SQLiteDataMaintenance(database, authentication_key=b"short")
    database.close()


def test_restore_retains_checkpoint_key_and_resumes_authenticated_workflow(tmp_path):
    from af.framework.context import ExecutionContext
    from af.infrastructure.local_keys import workflow_key
    from af.infrastructure.workflow_factory import create_workflow_service, reference_composition

    database = Database(f"sqlite:///{tmp_path / 'source.db'}")
    key = workflow_key(database.engine.url)
    service = create_workflow_service(database, authentication_key=key)
    context = ExecutionContext()
    run = service.submit(
        reference_composition().definition, b'{"texts":["strawberry"],"character":"r"}', context
    )
    service.resume(run.run_id, context, max_nodes=1)
    maintenance = SQLiteDataMaintenance(database, authentication_key=key)
    backup = maintenance.backup(tmp_path / "backup.db")
    restored_path = maintenance.restore(backup.path, tmp_path / "restored.db")
    restored = Database(f"sqlite:///{restored_path}")
    assert workflow_key(restored.engine.url) == key
    restored_service = create_workflow_service(
        restored, authentication_key=workflow_key(restored.engine.url)
    )
    result = restored_service.resume(run.run_id, context)
    assert result.nodes[-1].output == b'{"total":3}'
    restored.close()
    database.close()


def test_restore_failure_preserves_replaced_key_sidecar(tmp_path, monkeypatch):
    import af.infrastructure.data_maintenance as module

    database = Database("sqlite:///:memory:")
    maintenance = SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    backup = maintenance.backup(tmp_path / "backup.db")
    destination = tmp_path / "restored.db"
    key = destination.with_suffix(".workflow-key")

    def replace_key_then_fail(source, target):
        replacement = tmp_path / "replacement-key"
        replacement.write_bytes(b"another-owner")
        replacement.replace(key)
        raise OSError("publication failed")

    monkeypatch.setattr(module.os, "link", replace_key_then_fail)
    with pytest.raises(OSError, match="publication failed"):
        maintenance.restore(backup.path, destination)
    assert key.read_bytes() == b"another-owner"
    assert not destination.exists()
    database.close()


def test_backup_failure_preserves_replaced_destination(tmp_path, monkeypatch):
    import af.infrastructure.data_maintenance as module

    database = Database("sqlite:///:memory:")
    maintenance = SQLiteDataMaintenance(database, authentication_key=b"k" * 32)
    destination = tmp_path / "backup.db"

    def replace_backup_then_fail(path, payload):
        replacement = tmp_path / "replacement-backup"
        replacement.write_bytes(b"another-owner")
        replacement.replace(destination)
        raise OSError("manifest failed")

    monkeypatch.setattr(module, "_private_write", replace_backup_then_fail)
    with pytest.raises(OSError, match="manifest failed"):
        maintenance.backup(destination)
    assert destination.read_bytes() == b"another-owner"
    database.close()
