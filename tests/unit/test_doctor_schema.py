from __future__ import annotations

import hashlib
import sqlite3

from af.config import Settings
from af.infrastructure.database import Database
from af.infrastructure.doctor import diagnose


def test_readonly_doctor_rejects_unversioned_schema_without_creating_files(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE af_tasks (id TEXT)")
        connection.execute("CREATE TABLE af_run_traces (id TEXT)")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    files = set(tmp_path.iterdir())
    result = diagnose(Settings(database_url=f"sqlite:///{path}"))
    assert result["checks"]["storage"] == "schema_unversioned"
    assert not result["ready"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert set(tmp_path.iterdir()) == files


def test_doctor_reports_exact_migration_fingerprint_and_detects_column_drift(tmp_path):
    path = tmp_path / "state.db"
    database = Database(f"sqlite:///{path}")
    database.close()
    settings = Settings(database_url=f"sqlite:///{path}")
    ready = diagnose(settings)
    assert ready["checks"]["storage"] == "ready"
    assert ready["reference_schema"]["fingerprint"]
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE af_tasks ADD COLUMN extra TEXT")
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = diagnose(settings)
    assert result["checks"]["storage"] == "schema_drift"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
