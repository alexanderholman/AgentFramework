"""Shared use cases for explicit local backup, restore and retention."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from af.framework.data_lifecycle import DataMaintenance


class DataMaintenanceController:
    def __init__(self, service: DataMaintenance) -> None:
        self._service = service

    def initialize(self) -> dict[str, object]:
        status = self._service.initialize()
        return {
            "schema_version": "1.0.0",
            "migrations": list(status.migrations),
            "fingerprint": status.fingerprint,
            "verified": True,
        }

    def backup(self, destination: Path) -> dict[str, object]:
        receipt = self._service.backup(destination)
        return {
            "path": str(receipt.path),
            "manifest": str(receipt.manifest_path),
            "sha256": receipt.sha256,
            "size_bytes": receipt.size_bytes,
            "created_at": receipt.created_at.isoformat(),
        }

    def restore(self, backup: Path, destination: Path) -> dict[str, object]:
        path = self._service.restore(backup, destination)
        return {"path": str(path), "restored": True}

    def prune(self, *, before: datetime, execute: bool = False) -> dict[str, object]:
        result = self._service.retain(before=before, dry_run=not execute)
        return {
            "before": result.before.isoformat(),
            "dry_run": result.dry_run,
            "records": dict(result.removed),
            "scope": "tasks, traces, terminal program jobs; backups retained separately",
        }
