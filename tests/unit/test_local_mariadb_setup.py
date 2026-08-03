from __future__ import annotations

import stat
from pathlib import Path

import pytest

from af.application.database_setup import (
    DatabaseSetupError,
    DatabaseSetupMode,
    DatabaseSetupRequest,
)
from af.infrastructure import db_setup as db_setup_module
from af.infrastructure.db_setup import (
    AtomicEnvFileWriter,
    LocalMariaDBProvisioner,
    MariaDBConnectionVerifier,
)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, str] | None, bytes | None]] = []

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> None:
        self.calls.append((command, env, stdin))


def _request(tmp_path: Path, **overrides: object) -> DatabaseSetupRequest:
    fields = {
        "mode": DatabaseSetupMode.LOCAL_DEBIAN,
        "user": "af_test",
        "password": "pa'ss;word",
        "database": "af_test",
        "host": "127.0.0.1",
        "port": 3306,
        "project_root": tmp_path,
        "execute": True,
    }
    fields.update(overrides)
    return DatabaseSetupRequest(**fields)


def test_local_mariadb_provisioner_fails_closed_on_unsupported_platform(tmp_path: Path) -> None:
    provisioner = LocalMariaDBProvisioner(
        runner=RecordingRunner(),
        os_release=lambda: {"ID": "fedora", "ID_LIKE": "fedora rhel"},
    )

    with pytest.raises(DatabaseSetupError, match="Debian or Ubuntu"):
        provisioner.preview(_request(tmp_path))


def test_local_mariadb_provisioner_uses_safe_idempotent_sql_and_supported_commands(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    provisioner = LocalMariaDBProvisioner(
        runner=runner,
        os_release=lambda: {"ID": "ubuntu", "ID_LIKE": "debian"},
    )

    plan = provisioner.apply(_request(tmp_path))

    assert plan.actions[0] == "install MariaDB packages with apt-get"
    assert runner.calls[0] == (
        ("sudo", "apt-get", "install", "-y", "mariadb-server", "mariadb-client"),
        {"DEBIAN_FRONTEND": "noninteractive"},
        None,
    )
    assert runner.calls[1] == (("sudo", "systemctl", "enable", "--now", "mariadb"), None, None)
    sql_command = runner.calls[2][0]
    assert sql_command == ("sudo", "mariadb")
    sql = runner.calls[2][2].decode("utf-8")
    assert "CREATE DATABASE IF NOT EXISTS `af_test`" in sql
    assert "CREATE USER IF NOT EXISTS 'af_test'@'127.0.0.1' IDENTIFIED BY 'pa''ss;word'" in sql
    assert "ALTER USER 'af_test'@'127.0.0.1' IDENTIFIED BY 'pa''ss;word'" in sql
    assert "GRANT ALL PRIVILEGES ON `af_test`.* TO 'af_test'@'127.0.0.1'" in sql
    assert "DROP DATABASE" not in sql


def test_local_mariadb_provisioner_escapes_backslashes_in_sql_literals(tmp_path: Path) -> None:
    runner = RecordingRunner()
    provisioner = LocalMariaDBProvisioner(
        runner=runner,
        os_release=lambda: {"ID": "ubuntu", "ID_LIKE": "debian"},
    )

    provisioner.apply(_request(tmp_path, password="pa\\'ss"))

    sql = runner.calls[2][2].decode("utf-8")
    assert "IDENTIFIED BY 'pa\\\\''ss'" in sql


def test_atomic_env_file_writer_preserves_existing_content_and_creates_backup(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=1\nKEEP=2\n", encoding="utf-8")
    env_file.chmod(0o600)

    result = AtomicEnvFileWriter().update(
        env_file=env_file,
        key="AF_MARIADB_TEST_URL",
        value="mariadb+pymysql://af_test:encoded@127.0.0.1:3306/af_test",
    )

    assert result.changed is True
    assert result.backup_file == str(tmp_path / ".env.bak")
    assert stat.S_IMODE((tmp_path / ".env.bak").stat().st_mode) == 0o600
    assert env_file.read_text(encoding="utf-8") == (
        "EXISTING=1\nKEEP=2\n"
        "AF_MARIADB_TEST_URL=mariadb+pymysql://af_test:encoded@127.0.0.1:3306/af_test\n"
    )
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == "EXISTING=1\nKEEP=2\n"


def test_atomic_env_file_writer_updates_existing_value_without_clobbering_other_settings(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EXISTING=1\nAF_MARIADB_TEST_URL=old\nKEEP=2\n",
        encoding="utf-8",
    )

    result = AtomicEnvFileWriter().update(
        env_file=env_file,
        key="AF_MARIADB_TEST_URL",
        value="mariadb+pymysql://af_test:new@127.0.0.1:3306/af_test",
    )

    assert result.changed is True
    assert env_file.read_text(encoding="utf-8") == (
        "EXISTING=1\n"
        "AF_MARIADB_TEST_URL=mariadb+pymysql://af_test:new@127.0.0.1:3306/af_test\n"
        "KEEP=2\n"
    )


def test_atomic_env_file_writer_returns_no_backup_for_idempotent_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    content = "AF_MARIADB_TEST_URL=mariadb+pymysql://af_test:same@127.0.0.1:3306/af_test\n"
    env_file.write_text(content, encoding="utf-8")

    result = AtomicEnvFileWriter().update(
        env_file=env_file,
        key="AF_MARIADB_TEST_URL",
        value="mariadb+pymysql://af_test:same@127.0.0.1:3306/af_test",
    )

    assert result.changed is False
    assert result.backup_file is None
    assert env_file.read_text(encoding="utf-8") == content


def test_atomic_env_file_writer_rejects_duplicate_keys_and_preserves_original(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    original = "AF_MARIADB_TEST_URL=one\nAF_MARIADB_TEST_URL=two\n"
    env_file.write_text(original, encoding="utf-8")

    with pytest.raises(DatabaseSetupError, match="duplicate AF_MARIADB_TEST_URL entries"):
        AtomicEnvFileWriter().update(
            env_file=env_file,
            key="AF_MARIADB_TEST_URL",
            value="mariadb+pymysql://af_test:new@127.0.0.1:3306/af_test",
        )
    assert env_file.read_text(encoding="utf-8") == original


def test_atomic_env_file_writer_keeps_original_file_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    original = "EXISTING=1\n"
    env_file.write_text(original, encoding="utf-8")

    def fail_replace(self: Path, target: Path) -> Path:
        del target
        if self == env_file:
            return self
        raise OSError("disk full")

    monkeypatch.setattr(db_setup_module.Path, "replace", fail_replace)

    with pytest.raises(DatabaseSetupError, match="could not update environment file"):
        AtomicEnvFileWriter().update(
            env_file=env_file,
            key="AF_MARIADB_TEST_URL",
            value="mariadb+pymysql://af_test:new@127.0.0.1:3306/af_test",
        )
    assert env_file.read_text(encoding="utf-8") == original


def test_atomic_env_file_writer_rejects_non_file_env_path(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.mkdir()

    with pytest.raises(DatabaseSetupError, match="could not update environment file"):
        AtomicEnvFileWriter().update(
            env_file=env_path,
            key="AF_MARIADB_TEST_URL",
            value="mariadb+pymysql://af_test:new@127.0.0.1:3306/af_test",
        )


def test_atomic_env_file_writer_rejects_invalid_key_and_value(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"

    with pytest.raises(DatabaseSetupError, match="environment key is invalid"):
        AtomicEnvFileWriter().update(
            env_file=env_file,
            key="",
            value="mariadb+pymysql://af_test:new@127.0.0.1:3306/af_test",
        )
    with pytest.raises(DatabaseSetupError, match="environment value is invalid"):
        AtomicEnvFileWriter().update(
            env_file=env_file,
            key="AF_MARIADB_TEST_URL",
            value="",
        )


def test_atomic_env_file_writer_rejects_missing_parent_directory(tmp_path: Path) -> None:
    env_file = tmp_path / "missing" / ".env"

    with pytest.raises(DatabaseSetupError, match="directory is unavailable"):
        AtomicEnvFileWriter().update(
            env_file=env_file,
            key="AF_MARIADB_TEST_URL",
            value="mariadb+pymysql://af_test:new@127.0.0.1:3306/af_test",
        )


def test_mariadb_connection_verifier_uses_database_manager_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, object]] = []

    class FakeTransaction:
        def __enter__(self) -> FakeTransaction:
            events.append(("enter", None))
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            del exc_type, exc, traceback
            events.append(("exit", None))

        def query(self, statement):  # type: ignore[no-untyped-def]
            events.append(("query", statement.sql))
            return [{"version": "10.11.8-MariaDB"}]

    class FakeManager:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            events.append(("init", kwargs["connections"]))

        def install(self, registration) -> None:  # type: ignore[no-untyped-def]
            events.append(("install", registration.driver_name))

        def transaction(self, name=None, *, read_only=False, requires=()):  # type: ignore[no-untyped-def]
            del name, requires
            events.append(("transaction", read_only))
            return FakeTransaction()

        def close(self) -> None:
            events.append(("close", None))

    class FakeRegistration:
        driver_name = "mariadb"

    monkeypatch.setattr(db_setup_module, "DatabaseManager", FakeManager)
    monkeypatch.setattr(db_setup_module, "mariadb_registration", lambda: FakeRegistration())

    version = MariaDBConnectionVerifier().verify(
        url="mariadb+pymysql://af_test:encoded@127.0.0.1:3306/af_test"
    )

    assert version == "10.11.8-MariaDB"
    assert events == [
        (
            "init",
            {
                "primary": {
                    "driver": "mariadb",
                    "url": "mariadb+pymysql://af_test:encoded@127.0.0.1:3306/af_test",
                }
            },
        ),
        ("install", "mariadb"),
        ("transaction", True),
        ("enter", None),
        ("query", "SELECT VERSION() AS version"),
        ("exit", None),
        ("close", None),
    ]


def test_mariadb_connection_verifier_rejects_invalid_version_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTransaction:
        def __enter__(self) -> FakeTransaction:
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            del exc_type, exc, traceback

        def query(self, statement):  # type: ignore[no-untyped-def]
            del statement
            return [{"version": None}]

    class FakeManager:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del kwargs

        def install(self, registration) -> None:  # type: ignore[no-untyped-def]
            del registration

        def transaction(self, name=None, *, read_only=False, requires=()):  # type: ignore[no-untyped-def]
            del name, read_only, requires
            return FakeTransaction()

        def close(self) -> None:
            return None

    class FakeRegistration:
        driver_name = "mariadb"

    monkeypatch.setattr(db_setup_module, "DatabaseManager", FakeManager)
    monkeypatch.setattr(db_setup_module, "mariadb_registration", lambda: FakeRegistration())

    with pytest.raises(DatabaseSetupError, match="invalid server version"):
        MariaDBConnectionVerifier().verify(
            url="mariadb+pymysql://af_test:encoded@127.0.0.1:3306/af_test"
        )


def test_mariadb_connection_verifier_translates_unexpected_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingManager:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            del kwargs

        def install(self, registration) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError(registration.driver_name)

        def close(self) -> None:
            return None

    class FakeRegistration:
        driver_name = "mariadb"

    monkeypatch.setattr(db_setup_module, "DatabaseManager", FailingManager)
    monkeypatch.setattr(db_setup_module, "mariadb_registration", lambda: FakeRegistration())

    with pytest.raises(DatabaseSetupError, match="could not verify the MariaDB connection"):
        MariaDBConnectionVerifier().verify(
            url="mariadb+pymysql://af_test:encoded@127.0.0.1:3306/af_test"
        )
