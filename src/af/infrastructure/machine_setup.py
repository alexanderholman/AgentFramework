"""Read-only discovery and bounded local SQLite capability provisioning.

No package installer, model download, network probe, listener or daemon is used.
The per-directory flock serializes apply; durable receipts reconcile interruption.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
import shutil
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from af.application.machine_setup import (
    CAPABILITY_ORDER,
    PROFILE_VERSION,
    MachineSetupPlan,
    MachineSetupRequest,
    expand_capabilities,
)
from af.cache.contracts import CacheKey, CacheNamespace
from af.cache.database import DatabaseCacheDriver
from af.config import Settings
from af.framework.types import ProcedureDescriptor, SelectionDecision, SelectorLocality
from af.infrastructure.database import Database
from af.infrastructure.scheduling import SQLiteScheduleHistory, load_schedule_definitions
from af.infrastructure.sqlite import SQLiteDriver
from af.messaging import EnvelopeKind, MessageEnvelope, MessageMetadata
from af.queue import DatabaseQueueDriver, QueueJob

_OWNED = (
    "af.sqlite3",
    "af.sqlite3-wal",
    "af.sqlite3-shm",
    "af.workflow-key",
    "machine.json",
    "schedules.json",
    "setup.sqlite3",
    "setup.sqlite3-wal",
    "setup.sqlite3-shm",
    "setup.lock",
)
_TABLES = {
    "core": {"af_tasks", "af_run_traces", "af_migrations"},
    "database": {"af_tasks", "af_migrations"},
    "queue": {"af_queue_jobs"},
    "cache": {"af_cache_entries", "af_cache_locks"},
    "scheduler": {"af_schedule_runs"},
}


def _safe(root: Path) -> None:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("machine directory is not a safe local directory")
    if any((root / name).is_symlink() for name in _OWNED):
        raise ValueError("machine managed paths must not be symbolic links")


def _read_configuration(root: Path) -> tuple[str | None, dict[str, object]]:
    _safe(root)
    path = root / "machine.json"
    if not path.exists():
        return None, {}
    with path.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576:
        raise ValueError("machine configuration exceeds its size bound")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("machine configuration JSON is invalid") from None
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "af.machine/1"
        or value.get("locality") != "local-only"
        or not isinstance(value.get("capabilities"), list)
        or any(
            type(capability) is not str or capability not in CAPABILITY_ORDER
            for capability in value["capabilities"]
        )
    ):
        raise ValueError("machine configuration is unsupported or invalid")
    enabled = tuple(value["capabilities"])
    if "core" not in enabled:
        raise ValueError("machine configuration requires its core capability")
    if len(set(enabled)) != len(enabled) or set(expand_capabilities(enabled)) != set(enabled):
        raise ValueError("machine configuration dependencies are incomplete")
    if value.get("remote_routes") not in (None, []):
        raise ValueError("machine configuration cannot implicitly enable remote routes")
    return hashlib.sha256(raw).hexdigest(), value


def machine_settings(root: Path) -> Settings:
    _, config = _read_configuration(root)
    if not config:
        raise ValueError("machine is not initialized")
    capabilities = cast(list[str], config["capabilities"])
    return replace(
        Settings.from_environment(),
        database_url=f"sqlite+pysqlite:///{root / 'af.sqlite3'}",
        allow_remote_inference=False,
        decision_enabled=False,
        selector_provider="disabled",
        schedules_path=str(root / "schedules.json") if "scheduler" in capabilities else None,
    )


def _write_json(path: Path, value: object) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="machine-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _tables(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA quick_check(1)").fetchone() != ("ok",):
            raise ValueError("machine database integrity check failed")
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }


class _NoModelSelector:
    name = "machine-offline-verification"
    locality = SelectorLocality.LOCAL

    def __init__(self) -> None:
        self.calls = 0

    def select(self, prompt: str, catalogue: tuple[ProcedureDescriptor, ...]) -> SelectionDecision:
        self.calls += 1
        raise RuntimeError("offline setup must not invoke a model selector")


class LocalMachineSetup:
    def export(self, root: Path) -> dict[str, object]:
        _, config = _read_configuration(root)
        if not config:
            raise ValueError("machine is not initialized")
        return config

    def configuration(self, root: Path) -> tuple[str | None, tuple[str, ...]]:
        digest, value = _read_configuration(root)
        return digest, tuple(cast(list[str], value.get("capabilities", [])))

    def discover(self, request: MachineSetupRequest) -> dict[str, object]:
        root = request.state_dir
        blocked = []
        try:
            _safe(root)
        except ValueError:
            blocked.append("unsafe-state-directory")
        existing = root
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        usage = shutil.disk_usage(existing)
        try:
            memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // 1_048_576
        except (ValueError, OSError, AttributeError):
            memory = None
        if platform.system() not in ("Linux", "Darwin"):
            blocked.append("unsupported-host-locking")
        if usage.free < (request.minimum_free_disk_mib + 16) * 1_048_576:
            blocked.append("insufficient-free-disk")
        if memory is not None and request.max_memory_mib > memory:
            blocked.append("insufficient-host-memory")
        models = []
        for candidate in request.model_paths:
            path = candidate.expanduser().absolute()
            valid = (
                path.is_file()
                and not path.is_symlink()
                and path.suffix.lower() in (".gguf", ".safetensors")
            )
            models.append(
                {
                    "path": str(path),
                    "format": path.suffix.lower(),
                    "state": "discovered-unverified" if valid else "unsupported-or-missing",
                    "bytes": path.stat().st_size if valid else None,
                }
            )
            if not valid:
                blocked.append("model-path-unsupported-or-missing")
        engines = []
        for candidate in request.engine_paths:
            path = candidate.expanduser().absolute()
            valid = path.is_file() and os.access(path, os.X_OK) and not path.is_symlink()
            engines.append(
                {
                    "path": str(path),
                    "state": "discovered-unverified" if valid else "unsupported-or-missing",
                }
            )
            if not valid:
                blocked.append("engine-path-unsupported-or-missing")
        gpu = []
        for index in range(16):
            device = Path(f"/sys/class/drm/card{index}/device")
            if device.exists():
                info = {}
                for field in ("vendor", "device", "mem_info_vram_total"):
                    try:
                        info[field] = (device / field).read_text()[:128].strip()
                    except OSError:
                        info[field] = "unknown"
                gpu.append(info)
        try:
            tables = sorted(_tables(root / "af.sqlite3"))
            database_state = "reusable" if tables else "not-initialized"
        except (OSError, sqlite3.Error, ValueError):
            tables, database_state = [], "invalid"
            blocked.append("database-unrecognized-or-corrupt")
        return {
            "os": platform.system(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "ram_mib": memory,
            "free_disk_mib": usage.free // 1_048_576,
            "gpu_devices": gpu,
            "gpu_qualification": "not-performed",
            "models": models,
            "engines": engines,
            "available_engine_executables": {
                name: shutil.which(name) for name in ("llama-server", "ollama", "vllm")
            },
            "database": {
                "path": str(root / "af.sqlite3"),
                "state": database_state,
                "tables": tables,
            },
            "network": "no network discovery or egress requested",
            "service_ports_probed": [],
            "blocked": sorted(set(blocked)),
        }

    @contextmanager
    def _exclusive(self, root: Path) -> Iterator[None]:
        import fcntl

        _safe(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(
            root / "setup.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another machine setup owns this directory") from None
            yield
        finally:
            os.close(descriptor)

    def _record(self, root: Path, receipt: dict[str, object]) -> None:
        with sqlite3.connect(root / "setup.sqlite3", timeout=5) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS machine_receipts "
                "(id TEXT PRIMARY KEY,state TEXT NOT NULL,payload TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR REPLACE INTO machine_receipts VALUES (?,?,?)",
                (receipt["id"], receipt["state"], json.dumps(receipt, sort_keys=True)),
            )

    def _reconcile(self, root: Path) -> list[str]:
        path = root / "setup.sqlite3"
        if not path.exists():
            return []
        recovered = []
        with sqlite3.connect(path, timeout=5) as connection:
            for identifier, payload in connection.execute(
                "SELECT id,payload FROM machine_receipts WHERE state='running'"
            ):
                value = json.loads(payload)
                value["state"] = "interrupted"
                value["recovery"] = "rerun reconciliation; existing data retained"
                connection.execute(
                    "UPDATE machine_receipts SET state=?,payload=? WHERE id=?",
                    ("interrupted", json.dumps(value), identifier),
                )
                recovered.append(identifier)
        return recovered

    def apply(self, plan: MachineSetupPlan) -> dict[str, object]:
        request, payload = plan.request, plan.to_dict()
        if self.discover(request)["blocked"]:
            raise ValueError("machine discovery changed or remains blocked")
        root = request.state_dir
        # Check before creating a directory; check again after acquiring the owner lock.
        if self.configuration(root)[0] != payload["previous_configuration_digest"]:
            raise ValueError("machine plan is stale; review a new plan")
        with self._exclusive(root):
            if self.configuration(root)[0] != payload["previous_configuration_digest"]:
                raise ValueError("machine plan is stale; review a new plan")
            receipt: dict[str, object] = {
                "id": secrets.token_hex(16),
                "schema_version": "af.machine-receipt/1",
                "plan_digest": plan.digest,
                "state": "running",
                "completed": [],
                "pending": payload["capabilities"],
                "rolled_back": [],
                "reconciled_interruptions": self._reconcile(root),
                "retention": "all existing data retained",
                "started_at": datetime.now(UTC).isoformat(),
            }
            self._record(root, receipt)
            completed: list[str] = []
            capabilities = cast(list[str], payload["capabilities"])
            try:
                for capability in capabilities:
                    self._provision(capability, root)
                    completed.append(capability)
                    receipt["completed"] = list(completed)
                    receipt["pending"] = [item for item in capabilities if item not in completed]
                    self._record(root, receipt)
                receipt["verification"] = self._verify(root)
                config = {
                    "schema_version": "af.machine/1",
                    "profiles": payload["profiles"],
                    "capabilities": capabilities,
                    "locality": "local-only",
                    "resource_budget": payload["resource_budget"],
                    "startup": "on-demand",
                    "configuration_version": PROFILE_VERSION,
                    "secrets": {
                        "workflow_key": {"provider": "private-local-file", "key": "af.workflow-key"}
                    },
                    "models": cast(dict[str, object], payload["discovery"])["models"],
                    "engines": cast(dict[str, object], payload["discovery"])["engines"],
                }
                _write_json(root / "machine.json", config)
                receipt["state"] = "completed"
            except Exception:
                receipt["state"] = "failed"
                receipt["recovery"] = (
                    "rerun the plan after resolving the failed step; data retained"
                )
                receipt["failure"] = "local-provisioning-or-verification-failed"
            receipt["finished_at"] = datetime.now(UTC).isoformat()
            self._record(root, receipt)
            return receipt

    def _provision(self, capability: str, root: Path) -> None:
        path = root / "af.sqlite3"
        if not path.exists():
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        url = f"sqlite+pysqlite:///{path}"
        if capability in ("core", "database"):
            database = Database(url)
            database.close()
        elif capability == "queue":
            driver = DatabaseQueueDriver(SQLiteDriver(path), clock=lambda: datetime.now(UTC))
            try:
                identifier = "setup-" + secrets.token_hex(12)
                job = QueueJob(
                    MessageEnvelope.create(
                        metadata=MessageMetadata(
                            message_id=identifier,
                            message_type="setup.verify",
                            schema_version=1,
                            kind=EnvelopeKind.JOB,
                            occurred_at=datetime.now(UTC),
                        ),
                        payload={},
                    ),
                    lane=identifier,
                )
                driver.dispatch(job)
                claim = driver.reserve(lane=identifier)
                if claim is None or claim.job != job:
                    raise ValueError("queue readiness failed")
                driver.ack(claim)
            finally:
                driver.close()
        elif capability == "cache":
            cache = DatabaseCacheDriver(url=url)
            namespace, key = (
                CacheNamespace("af", "setup", "verification"),
                CacheKey(secrets.token_hex(12)),
            )
            try:
                cache.set(namespace, key, b"verified", ttl=timedelta(seconds=5))
                if cache.get(namespace, key) != b"verified":
                    raise ValueError("cache readiness failed")
                cache.delete(namespace, key)
            finally:
                cache.close()
        elif capability == "scheduler":
            SQLiteScheduleHistory(path)
            schedules = root / "schedules.json"
            if not schedules.exists():
                _write_json(schedules, {"schema": "af.schedules/1", "schedules": []})
            load_schedule_definitions(schedules)
        else:
            raise ValueError("unsupported local capability")

    @staticmethod
    def _verify(root: Path) -> dict[str, object]:
        from af.bootstrap import create_application

        selector = _NoModelSelector()
        application = create_application(
            settings=Settings(
                database_url=f"sqlite+pysqlite:///{root / 'af.sqlite3'}",
                selector_provider="disabled",
                allow_remote_inference=False,
                decision_enabled=False,
            ),
            selector=selector,
        )
        try:
            result = application.tasks.execute("how many rs in strawberry")
            if not result.verified or result.output != "3" or selector.calls != 0:
                raise ValueError("offline request verification failed")
            return {
                "verified": True,
                "output": "3",
                "selector_calls": selector.calls,
                "provider_calls": 0,
                "provider_routes": "disabled",
                "trace_id": result.trace_id,
            }
        finally:
            application.shutdown()

    def status(self, root: Path) -> dict[str, object]:
        try:
            digest, config = _read_configuration(root)
            tables = _tables(root / "af.sqlite3")
        except (ValueError, OSError, sqlite3.Error):
            return {
                "state": "misconfigured",
                "repair": "inspect local files; rerun af init --plan",
                "capabilities": {},
            }
        enabled = cast(list[str], config.get("capabilities", []))
        capabilities = {
            name: {
                "enabled": name in enabled,
                "state": ("healthy" if _TABLES[name] <= tables else "degraded")
                if name in enabled
                else "disabled",
                "backend": "installed-python" if name == "core" else "local-sqlite",
                "retention": "retained",
                "version": PROFILE_VERSION,
            }
            for name in CAPABILITY_ORDER
        }
        receipts = []
        journal = root / "setup.sqlite3"
        if journal.exists():
            with sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True, timeout=1) as connection:
                receipts = [
                    {"id": row[0], "state": row[1]}
                    for row in connection.execute(
                        "SELECT id,state FROM machine_receipts ORDER BY rowid DESC LIMIT 20"
                    )
                ]
        return {
            "schema_version": "af.machine-status/1",
            "state": (
                "healthy"
                if all(item["state"] != "degraded" for item in capabilities.values())
                else "degraded"
            )
            if config
            else "not-initialized",
            "configuration_digest": digest,
            "capabilities": capabilities,
            "receipts": receipts,
            "locality": "local-only",
            "model_routes_enabled": False,
            "startup": "on-demand",
            "repair": "af init --plan to review reconciliation",
            "state_dir": str(root),
        }

    def disable(self, root: Path, capability: str) -> dict[str, object]:
        if not _read_configuration(root)[1]:
            raise ValueError("machine is not initialized")
        with self._exclusive(root):
            _, config = _read_configuration(root)
            if not config:
                raise ValueError("machine is not initialized")
            enabled = cast(list[str], config["capabilities"])
            if any(
                name != capability and capability in expand_capabilities((name,))
                for name in enabled
            ):
                raise ValueError(
                    "enabled capabilities depend on this capability; disable dependents first"
                )
            config["capabilities"] = [name for name in enabled if name != capability]
            _write_json(root / "machine.json", config)
            receipt: dict[str, object] = {
                "id": secrets.token_hex(16),
                "schema_version": "af.machine-receipt/1",
                "state": "completed",
                "action": "disable",
                "capability": capability,
                "retention": "all data retained",
                "external_services_stopped": False,
                "finished_at": datetime.now(UTC).isoformat(),
            }
            self._record(root, receipt)
            return receipt
