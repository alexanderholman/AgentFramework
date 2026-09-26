"""Non-mutating local readiness probes; no connections to remote services."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from af.config import Settings
from af.framework.migrations import (
    MigrationIntegrityError,
    MigrationRunner,
    MigrationVerificationError,
)
from af.framework.relational import DatabaseOperationError
from af.infrastructure.reference_schema import reference_migrations
from af.infrastructure.sqlite import SQLiteDriver


def diagnose(settings: Settings) -> dict[str, object]:
    checks = {
        "python": "ready" if sys.version_info >= (3, 12) else "unsupported",
        "storage": _storage_status(settings.database_url),
    }
    return {
        "schema_version": "1.0.0",
        "ready": all(value == "ready" for value in checks.values()),
        "checks": checks,
        "remote_inference": "configured" if settings.allow_remote_inference else "disabled",
        "production_certified": False,
        "reference_schema": {
            "migrations": [item.migration_id for item in reference_migrations()],
            "fingerprint": hashlib.sha256(
                ":".join(item.checksum for item in reference_migrations()).encode()
            ).hexdigest(),
        },
    }


def _storage_status(url: str) -> str:
    try:
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite":
            return "external_probe_required"
        if not parsed.database or parsed.database == ":memory:":
            return "ephemeral"
        path = Path(parsed.database).expanduser().resolve()
        if not path.is_file():
            return "not_initialized"
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
                return "integrity_failed"
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not {"af_tasks", "af_run_traces"} <= tables:
                return "schema_unavailable"
            if not {"af_migrations", "af_migration_locks"} <= tables:
                return "schema_unversioned"
            catalogue = reference_migrations()
            try:
                with SQLiteDriver(":memory:", connection=connection) as driver:
                    runner = MigrationRunner(
                        driver,
                        catalogue,
                        owner_id="readonly-doctor",
                        clock=lambda: datetime.now(UTC),
                    )
                    applied = runner.verify()
                    if applied != tuple(item.migration_id for item in catalogue):
                        return "schema_upgrade_required"
            except (MigrationIntegrityError, MigrationVerificationError):
                return "schema_drift"
        return "ready"
    except (OSError, ValueError, sqlite3.Error, DatabaseOperationError):
        return "unavailable"
