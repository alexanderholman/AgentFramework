"""Explicit local-machine planning, confirmation and retained-data reconciliation."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

CAPABILITY_ORDER = ("core", "database", "queue", "cache", "scheduler")
DEPENDENCIES = {
    "core": (),
    "database": ("core",),
    "queue": ("database",),
    "cache": ("database",),
    "scheduler": ("queue", "cache"),
}
PROFILE_VERSION = "1.0.0"


def expand_capabilities(profiles: tuple[str, ...]) -> tuple[str, ...]:
    selected = {"core"}

    def add(name: str) -> None:
        if name not in DEPENDENCIES:
            raise ValueError("unsupported machine profile or capability")
        selected.add(name)
        for dependency in DEPENDENCIES[name]:
            add(dependency)

    for profile in profiles:
        add(profile)
    return tuple(name for name in CAPABILITY_ORDER if name in selected)


@dataclass(frozen=True, slots=True)
class MachineSetupRequest:
    state_dir: Path
    profiles: tuple[str, ...] = ("core",)
    model_paths: tuple[Path, ...] = ()
    engine_paths: tuple[Path, ...] = ()
    max_memory_mib: int = 256
    minimum_free_disk_mib: int = 64

    def __post_init__(self) -> None:
        if not isinstance(self.state_dir, Path):
            raise ValueError("machine state directory must be an explicit path")
        object.__setattr__(self, "state_dir", self.state_dir.expanduser().absolute())
        if (
            type(self.profiles) is not tuple
            or not self.profiles
            or len(self.profiles) > 5
            or len(set(self.profiles)) != len(self.profiles)
        ):
            raise ValueError("machine profiles must be a nonempty distinct tuple")
        expand_capabilities(self.profiles)
        for paths in (self.model_paths, self.engine_paths):
            if (
                type(paths) is not tuple
                or len(paths) > 32
                or any(not isinstance(path, Path) for path in paths)
            ):
                raise ValueError("machine discovery paths must be bounded explicit paths")
        if type(self.max_memory_mib) is not int or not 64 <= self.max_memory_mib <= 1_048_576:
            raise ValueError("machine memory budget must be within 64..1048576 MiB")
        if (
            type(self.minimum_free_disk_mib) is not int
            or not 32 <= self.minimum_free_disk_mib <= 1_048_576
        ):
            raise ValueError("machine disk reserve must be within 32..1048576 MiB")

    @classmethod
    def from_declaration(cls, root: Path, declaration: Mapping[str, object]) -> MachineSetupRequest:
        if (
            declaration.get("schema_version") != "af.machine/1"
            or declaration.get("locality") != "local-only"
        ):
            raise ValueError("machine declaration version/locality is unsupported")
        capabilities = declaration.get("capabilities")
        budget = declaration.get("resource_budget")
        if (
            not isinstance(capabilities, list)
            or not all(type(item) is str for item in capabilities)
            or not isinstance(budget, dict)
        ):
            raise ValueError("machine declaration capabilities/budget are invalid")
        if declaration.get("remote_routes") not in (None, []):
            raise ValueError("this machine profile cannot authorize remote routes")
        inventories = []
        for field in ("models", "engines"):
            values = declaration.get(field, [])
            if (
                not isinstance(values, list)
                or len(values) > 32
                or any(
                    not isinstance(value, dict) or type(value.get("path")) is not str
                    for value in values
                )
            ):
                raise ValueError("machine declaration inventory is invalid")
            inventories.append(tuple(Path(value["path"]) for value in values))
        return cls(
            root,
            profiles=tuple(capabilities),
            model_paths=inventories[0],
            engine_paths=inventories[1],
            max_memory_mib=budget.get("max_memory_mib", 256),
            minimum_free_disk_mib=budget.get("minimum_free_disk_mib", 64),
        )


@dataclass(frozen=True, slots=True)
class MachineSetupPlan:
    request: MachineSetupRequest
    payload_json: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.payload_json.encode()).hexdigest()

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = json.loads(self.payload_json)
        return {**result, "plan_digest": self.digest}


class MachineSetupBackend(Protocol):
    def discover(self, request: MachineSetupRequest) -> dict[str, object]: ...
    def export(self, root: Path) -> dict[str, object]: ...
    def configuration(self, root: Path) -> tuple[str | None, tuple[str, ...]]: ...
    def apply(self, plan: MachineSetupPlan) -> dict[str, object]: ...
    def status(self, root: Path) -> dict[str, object]: ...
    def disable(self, root: Path, capability: str) -> dict[str, object]: ...


class MachineSetupController:
    def __init__(self, backend: MachineSetupBackend) -> None:
        self._backend = backend

    def plan(self, request: MachineSetupRequest) -> MachineSetupPlan:
        if not isinstance(request, MachineSetupRequest):
            raise TypeError("machine planning requires MachineSetupRequest")
        discovery = self._backend.discover(request)
        digest, existing = self._backend.configuration(request.state_dir)
        capabilities = expand_capabilities(tuple(set(request.profiles) | set(existing)))
        payload = {
            "schema_version": "af.machine-plan/1",
            "state_dir": str(request.state_dir),
            "profiles": {name: PROFILE_VERSION for name in sorted(request.profiles)},
            "capabilities": list(capabilities),
            "previous_configuration_digest": digest,
            "discovery": discovery,
            "blocked": discovery.get("blocked", []),
            "downloads_bytes": 0,
            "estimated_additional_disk_bytes": 16 * 1_048_576,
            "ports": [],
            "data_locations": {
                "database": str(request.state_dir / "af.sqlite3"),
                "configuration": str(request.state_dir / "machine.json"),
                "receipts": str(request.state_dir / "setup.sqlite3"),
                "private_key_reference": "private-local-file:af.workflow-key",
                "scheduler_definitions": str(request.state_dir / "schedules.json")
                if "scheduler" in capabilities
                else None,
            },
            "locality": "local-only",
            "remote_routes": [],
            "model_routes_enabled": False,
            "telemetry_egress": False,
            "startup": "on-demand; no daemon, public listener or boot service",
            "data_retention": "existing data, models and unrelated configuration are retained",
            "resource_budget": {
                "max_memory_mib": request.max_memory_mib,
                "minimum_free_disk_mib": request.minimum_free_disk_mib,
                "worker_concurrency": 1,
            },
            "steps": [
                {
                    "capability": name,
                    "version": PROFILE_VERSION,
                    "action": "reconcile" if name in existing else "enable",
                    "backend": "installed-python" if name == "core" else "local-sqlite",
                }
                for name in capabilities
            ],
            "verification": "offline strawberry request; selector/provider calls must be zero",
            "manual_actions": [
                "model inference remains disabled; register a pinned runtime separately"
            ],
        }
        return MachineSetupPlan(request, json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def apply(self, plan: MachineSetupPlan, *, confirmed: bool) -> dict[str, object]:
        if not isinstance(plan, MachineSetupPlan) or confirmed is not True:
            raise ValueError("machine setup requires an explicit approved plan")
        if plan.to_dict()["blocked"]:
            raise ValueError("machine setup is blocked; resolve the plan decisions first")
        return self._backend.apply(plan)

    def enable_plan(self, root: Path, capability: str) -> MachineSetupPlan:
        return self.plan(MachineSetupRequest(root, profiles=(capability,)))

    def disable(self, root: Path, capability: str, *, confirmed: bool) -> dict[str, object]:
        if confirmed is not True or capability not in CAPABILITY_ORDER or capability == "core":
            raise ValueError("disabling an optional capability requires explicit confirmation")
        return self._backend.disable(root.expanduser().absolute(), capability)

    def status(self, root: Path) -> dict[str, object]:
        return self._backend.status(root.expanduser().absolute())

    def export(self, root: Path) -> dict[str, object]:
        return self._backend.export(root.expanduser().absolute())
