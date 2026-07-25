"""Database service setup utilities for local development."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _run(
    cmd: list[str],
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a subprocess command, forwarding stdout/stderr."""
    return subprocess.run(cmd, check=check, env=env)


def _service_installed(service: str) -> bool:
    """Return True if the service binary is available on PATH."""
    return subprocess.run(
        ["systemctl", "is-active", service],
        check=False,
        capture_output=True,
    ).returncode == 0


def _service_installed_but_inactive(service: str) -> bool:
    """Return True if the service binary exists but is not running."""
    return subprocess.run(
        ["which", "mariadbd-safe"] if service == "mariadb" else ["which", service],
        check=False,
        capture_output=True,
    ).returncode == 0


def _install_mariadb_server() -> None:
    """Install MariaDB server via apt-get (Debian/Ubuntu)."""
    print("Installing MariaDB server packages...")
    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    result = _run(
        [
            "sudo",
            "apt-get",
            "install",
            "-y",
            "mariadb-server",
            "mariadb-client",
            "python3-pymysql",
        ],
        env=env,
    )
    if result.returncode != 0:
        print("Failed to install MariaDB packages.", file=sys.stderr)
        sys.exit(1)
    print("MariaDB packages installed successfully.")


def _start_mariadb_service() -> None:
    """Start the MariaDB service if it is not already running."""
    if _service_installed("mariadb"):
        print("MariaDB service is already running.")
        return
    print("Starting MariaDB service...")
    result = _run(["sudo", "systemctl", "start", "mariadb"])
    if result.returncode != 0:
        print("Failed to start MariaDB service.", file=sys.stderr)
        sys.exit(1)
    result = _run(["sudo", "systemctl", "enable", "mariadb"], check=False)
    if result.returncode != 0:
        print("Warning: could not enable MariaDB to start on boot.", file=sys.stderr)
    print("MariaDB service started successfully.")


def _create_test_database(user: str, password: str, database: str) -> None:
    """Create the test database and user via the mariadb client."""
    print(f"Creating MariaDB test database '{database}' and user '{user}'...")

    sql = f"""
CREATE DATABASE IF NOT EXISTS {database} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '{user}'@'localhost' IDENTIFIED BY '{password}';
GRANT ALL PRIVILEGES ON {database}.* TO '{user}'@'localhost';
FLUSH PRIVILEGES;
"""
    result = _run(["sudo", "mariadb", "-e", sql])
    if result.returncode != 0:
        print("Failed to create MariaDB test database/user.", file=sys.stderr)
        sys.exit(1)
    print(f"MariaDB test database '{database}' and user '{user}' created successfully.")


def _generate_connection_url(
    user: str, password: str, host: str, database: str, port: int = 3306,
) -> str:
    """Build the mariadb+pymysql connection URL."""
    return f"mariadb+pymysql://{user}:{password}@{host}:{port}/{database}"


def _write_env_url(url: str, env_file: Path) -> None:
    """Append the connection URL to a .env file if it doesn't already exist."""
    env_file = env_file.resolve()
    key = "AF_MARIADB_TEST_URL"

    if env_file.exists():
        content = env_file.read_text()
        if key in content:
            print(f"{key} already set in {env_file}, skipping.")
            return

    env_file.write_text(f"{key}={url}\n")
    print(f"Wrote {key} to {env_file}")


def setup_mariadb(
    user: str = "af_test",
    password: str = "af_test_pass",
    database: str = "af_test",
    host: str = "localhost",
    port: int = 3306,
    env_file: Path | None = None,
    project_root: Path | None = None,
) -> str:
    """Full MariaDB setup: install, start, create DB/user, return connection URL.

    Args:
        user: Database user for AF integration tests.
        password: Password for the test user.
        database: Database name for AF integration tests.
        host: Host to connect to.
        port: Port to connect on.
        env_file: Path to write the env var to. Defaults to .env in project_root.
        project_root: Project root directory (for default .env location).

    Returns:
        The mariadb+pymysql connection URL string.
    """
    # Step 1: Install if needed
    if not _service_installed("mariadb") and not _service_installed_but_inactive("mariadb"):
        _install_mariadb_server()

    # Step 2: Start service
    _start_mariadb_service()

    # Step 3: Create database and user
    _create_test_database(user, password, database)

    # Step 4: Build URL
    url = _generate_connection_url(user, password, host, database, port)

    # Step 5: Write to .env
    if env_file is None:
        if project_root is None:
            project_root = Path.cwd()
        env_file = project_root / ".env"
    _write_env_url(url, env_file)

    print("\nMariaDB setup complete!")
    print(f"Connection URL: {url}")
    print(f"Env file: {env_file}")
    print("\nRun tests with:")
    print(f'  export AF_MARIADB_TEST_URL="{url}"')
    print("  uv run pytest tests/integration/test_mariadb_relational.py")

    return url
