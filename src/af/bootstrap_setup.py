"""Lazy composition for read-only setup and explicitly configured local machines."""

from __future__ import annotations

import os
from pathlib import Path

from af.application.machine_setup import MachineSetupController
from af.bootstrap import AFApplication, create_application
from af.cache.database import database_cache_registration
from af.cache.manager import bind_cache_manager
from af.framework.container import ProviderRegistrar, ServiceProvider
from af.infrastructure.machine_setup import LocalMachineSetup, machine_settings
from af.infrastructure.sqlite import SQLiteDriver
from af.queue.database_manager import database_queue_registration
from af.queue.manager import bind_queue_manager


def machine_root() -> Path:
    return Path(os.environ.get("AF_MACHINE_ROOT", "storage")).expanduser().absolute()


def create_machine_setup_controller() -> MachineSetupController:
    """Construct inert adapters; discovery/planning never boots an application."""
    return MachineSetupController(LocalMachineSetup())


class MachineCapabilitiesProvider(ServiceProvider):
    provider_id = "af.machine-capabilities"
    dependencies = ("af.application",)

    def __init__(self, root: Path, capabilities: tuple[str, ...]) -> None:
        self._root, self._capabilities = root, capabilities

    def register(self, container: ProviderRegistrar) -> None:
        if "queue" in self._capabilities:
            bind_queue_manager(
                container,
                connections={"primary": {"driver": "database"}},
                default="primary",
                deployment_profile="local",
                policy_version="machine-v1",
                registrations=(
                    database_queue_registration(
                        database_factory=lambda: SQLiteDriver(self._root / "af.sqlite3")
                    ),
                ),
            )
        if "cache" in self._capabilities:
            bind_cache_manager(
                container,
                connections={
                    "primary": {
                        "driver": "database",
                        "url": f"sqlite+pysqlite:///{self._root / 'af.sqlite3'}",
                    }
                },
                default="primary",
                deployment_profile="local",
                policy_version="machine-v1",
                registrations=(database_cache_registration(),),
            )


def create_machine_application() -> AFApplication:
    """Installed application factory for the explicit machine declaration."""
    root = machine_root()
    _, capabilities = LocalMachineSetup().configuration(root)
    return create_application(
        settings=machine_settings(root),
        providers=(MachineCapabilitiesProvider(root, capabilities),),
    )
