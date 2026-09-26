"""Portable contracts for private data maintenance services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    path: Path
    manifest_path: Path
    sha256: str
    size_bytes: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RetentionResult:
    before: datetime
    dry_run: bool
    removed: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class SchemaStatus:
    migrations: tuple[str, ...]
    fingerprint: str


class DataMaintenance(Protocol):
    def initialize(self) -> SchemaStatus: ...
    def backup(self, destination: Path) -> BackupReceipt: ...
    def restore(self, backup: Path, destination: Path) -> Path: ...
    def retain(self, *, before: datetime, dry_run: bool = True) -> RetentionResult: ...
