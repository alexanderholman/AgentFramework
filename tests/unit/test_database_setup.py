from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from af.application.database_setup import (
    ConnectionUrlWriter,
    DatabaseConnectionVerifier,
    DatabaseSetupController,
    DatabaseSetupError,
    DatabaseSetupMode,
    DatabaseSetupPlan,
    DatabaseSetupRequest,
    DatabaseSetupService,
    EnvironmentFileUpdate,
    LocalDatabaseProvisioner,
)


@dataclass(frozen=True, slots=True)
class _Update:
    env_file: str
    backup_file: str | None
    changed: bool


class RecordingProvisioner(LocalDatabaseProvisioner):
    def __init__(self) -> None:
        self.preview_requests: list[DatabaseSetupRequest] = []
        self.apply_requests: list[DatabaseSetupRequest] = []

    def preview(self, request: DatabaseSetupRequest) -> DatabaseSetupPlan:
        self.preview_requests.append(request)
        return DatabaseSetupPlan(
            actions=(
                "install MariaDB packages with apt-get",
                "enable and start the mariadb service",
                "create or update the requested database and user",
            )
        )

    def apply(self, request: DatabaseSetupRequest) -> DatabaseSetupPlan:
        self.apply_requests.append(request)
        return self.preview(request)


class RecordingVerifier(DatabaseConnectionVerifier):
    def __init__(self) -> None:
        self.urls: list[str] = []

    def verify(self, *, url: str) -> str:
        self.urls.append(url)
        return "10.11.8-MariaDB"


class RecordingWriter(ConnectionUrlWriter):
    def __init__(self) -> None:
        self.calls: list[tuple[Path, str, str]] = []

    def update(self, *, env_file: Path, key: str, value: str) -> EnvironmentFileUpdate:
        self.calls.append((env_file, key, value))
        return EnvironmentFileUpdate(
            env_file=str(env_file),
            backup_file=str(env_file) + ".bak",
            changed=True,
        )


def _request(tmp_path: Path, **overrides: object) -> DatabaseSetupRequest:
    fields = {
        "mode": DatabaseSetupMode.LOCAL_DEBIAN,
        "user": "af_test",
        "password": "af_test_pass",
        "database": "af_test",
        "host": "127.0.0.1",
        "port": 3306,
        "project_root": tmp_path,
        "execute": False,
    }
    fields.update(overrides)
    return DatabaseSetupRequest(**fields)


def test_database_setup_service_previews_without_mutating_any_boundary(tmp_path: Path) -> None:
    provisioner = RecordingProvisioner()
    verifier = RecordingVerifier()
    writer = RecordingWriter()

    result = DatabaseSetupService(provisioner, verifier, writer).setup(_request(tmp_path))

    assert result.executed is False
    assert result.verified is False
    assert result.actions == (
        "install MariaDB packages with apt-get",
        "enable and start the mariadb service",
        "create or update the requested database and user",
    )
    assert provisioner.preview_requests == [_request(tmp_path)]
    assert provisioner.apply_requests == []
    assert verifier.urls == []
    assert writer.calls == []


def test_database_setup_service_executes_in_order_and_url_encodes_credentials(
    tmp_path: Path,
) -> None:
    provisioner = RecordingProvisioner()
    verifier = RecordingVerifier()
    writer = RecordingWriter()
    request = _request(tmp_path, password="pa:ss/word#1", execute=True)

    result = DatabaseSetupService(provisioner, verifier, writer).setup(request)

    assert result.executed is True
    assert result.verified is True
    assert provisioner.apply_requests == [request]
    assert result.actions == (
        "install MariaDB packages with apt-get",
        "enable and start the mariadb service",
        "create or update the requested database and user",
        "verify the MariaDB connection through DatabaseManager",
        "atomically update the project .env file",
    )
    assert verifier.urls == ["mariadb+pymysql://af_test:pa%3Ass%2Fword%231@127.0.0.1:3306/af_test"]
    assert writer.calls == [
        (
            tmp_path / ".env",
            "AF_MARIADB_TEST_URL",
            "mariadb+pymysql://af_test:pa%3Ass%2Fword%231@127.0.0.1:3306/af_test",
        )
    ]
    assert result.connection_url == "mariadb+pymysql://af_test:***@127.0.0.1:3306/af_test"


@pytest.mark.parametrize(
    ("user", "database"),
    [
        ("af_test;DROP", "af_test"),
        ("af_test", "af_test;DROP"),
        ("af-test", "af_test"),
    ],
)
def test_database_setup_service_rejects_unsafe_identifiers(
    tmp_path: Path,
    user: str,
    database: str,
) -> None:
    service = DatabaseSetupService(RecordingProvisioner(), RecordingVerifier(), RecordingWriter())

    with pytest.raises(DatabaseSetupError, match="letters, numbers, and underscores"):
        service.setup(_request(tmp_path, user=user, database=database, execute=True))


def test_database_setup_service_fails_closed_for_remote_hosts_in_local_mode(tmp_path: Path) -> None:
    service = DatabaseSetupService(RecordingProvisioner(), RecordingVerifier(), RecordingWriter())

    with pytest.raises(DatabaseSetupError, match="must use a local host"):
        service.setup(_request(tmp_path, host="db.example.com", execute=True))


def test_database_setup_service_rejects_invalid_root_port_and_password(tmp_path: Path) -> None:
    service = DatabaseSetupService(RecordingProvisioner(), RecordingVerifier(), RecordingWriter())

    with pytest.raises(DatabaseSetupError, match="existing directory"):
        service.setup(_request(tmp_path / "missing", execute=True))
    with pytest.raises(DatabaseSetupError, match="valid TCP port"):
        service.setup(_request(tmp_path, port=0, execute=True))
    with pytest.raises(DatabaseSetupError, match="must not be blank"):
        service.setup(_request(tmp_path, password="", execute=True))


def test_database_setup_service_rejects_non_string_host_and_non_boolean_execute(
    tmp_path: Path,
) -> None:
    service = DatabaseSetupService(RecordingProvisioner(), RecordingVerifier(), RecordingWriter())

    with pytest.raises(DatabaseSetupError, match="must use a local host"):
        service.setup(_request(tmp_path, host=object(), execute=True))
    with pytest.raises(DatabaseSetupError, match="execute flag is invalid"):
        service.setup(_request(tmp_path, execute=1))


def test_database_setup_service_rejects_non_request_values(tmp_path: Path) -> None:
    service = DatabaseSetupService(RecordingProvisioner(), RecordingVerifier(), RecordingWriter())

    with pytest.raises(TypeError, match="request is invalid"):
        service.setup(object())  # type: ignore[arg-type]


def test_database_setup_controller_rejects_invalid_service_or_result(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(TypeError, match="service is invalid"):
        DatabaseSetupController(object()).setup(request)

    class InvalidService:
        def setup(self, request: DatabaseSetupRequest) -> object:
            del request
            return object()

    with pytest.raises(TypeError, match="returned an invalid result"):
        DatabaseSetupController(InvalidService()).setup(request)
