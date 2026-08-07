"""Lightweight composition root for explicit local MariaDB setup."""

from __future__ import annotations

from af.application.database_setup import DatabaseSetupController
from af.framework.database_manager import DatabaseManager
from af.framework.relational import DatabaseFeature, DatabaseStatement
from af.infrastructure.local_mariadb_setup import LocalMariaDBSetupBackend, SubprocessCommandRunner
from af.infrastructure.mariadb_manager import mariadb_registration


def _verify_mariadb_connection(url: str) -> None:
    manager = DatabaseManager(
        connections={"primary": {"driver": "mariadb", "url": url}},
        default="primary",
        read="primary",
        write="primary",
        deployment_profile="local",
        policy_version="af-db-setup-v1",
    )
    manager.install(mariadb_registration())
    try:
        driver = manager.connection(
            "primary",
            requires=(DatabaseFeature.TRANSACTIONS, DatabaseFeature.SAVEPOINTS),
        )
        with driver.transaction(read_only=True) as transaction:
            rows = transaction.query(DatabaseStatement("SELECT 1 AS value", read_only=True))
        if len(rows) != 1 or rows[0]["value"] != 1:
            raise ValueError("MariaDB connection verification returned an unexpected result")
    finally:
        manager.close()


def create_database_setup_controller() -> DatabaseSetupController:
    """Compose local database setup without booting AF's application lifecycle."""

    return DatabaseSetupController(
        LocalMariaDBSetupBackend(
            command_runner=SubprocessCommandRunner(),
            connection_verifier=_verify_mariadb_connection,
        )
    )
