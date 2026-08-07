from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from af.framework.database_setup import (
    DatabaseSetupMode,
    DatabaseSetupRequest,
    DatabaseSetupStatus,
)
from af.infrastructure.local_mariadb_setup import DatabaseSetupError, LocalMariaDBSetupBackend


@dataclass(frozen=True, slots=True)
class _RecordedCommand:
    argv: tuple[str, ...]
    env: dict[str, str]
    input_text: str | None
    description: str


class _RecordingRunner:
    def __init__(self, *, fail_at: int | None = None, message: str = "failed") -> None:
        self.calls: list[_RecordedCommand] = []
        self._fail_at = fail_at
        self._message = message

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str] | None = None,
        input_text: str | None = None,
        description: str,
    ) -> None:
        self.calls.append(
            _RecordedCommand(
                argv=argv,
                env={} if env is None else dict(env),
                input_text=input_text,
                description=description,
            )
        )
        if self._fail_at is not None and len(self.calls) == self._fail_at:
            raise RuntimeError(self._message)


class _RecordingVerifier:
    def __init__(self, *, fail: bool = False, message: str = "verification failed") -> None:
        self.calls: list[str] = []
        self._fail = fail
        self._message = message

    def __call__(self, url: str) -> None:
        self.calls.append(url)
        if self._fail:
            raise ValueError(self._message)


def _backend(
    *,
    runner: _RecordingRunner | None = None,
    verifier: _RecordingVerifier | None = None,
    system: str = "Linux",
    os_release: str = "ID=ubuntu\nID_LIKE=debian\n",
) -> tuple[LocalMariaDBSetupBackend, _RecordingRunner, _RecordingVerifier]:
    resolved_runner = _RecordingRunner() if runner is None else runner
    resolved_verifier = _RecordingVerifier() if verifier is None else verifier
    backend = LocalMariaDBSetupBackend(
        command_runner=resolved_runner,
        connection_verifier=resolved_verifier,
        platform_system=lambda: system,
        os_release_loader=lambda: os_release,
    )
    return backend, resolved_runner, resolved_verifier


def test_database_setup_preview_is_redacted_and_side_effect_free(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AF_OTHER=value\n", encoding="utf-8")
    backend, runner, verifier = _backend()

    result = backend.setup(
        DatabaseSetupRequest(
            project_root=tmp_path,
            env_file=env_file,
            mode=DatabaseSetupMode.LOCAL_DEBIAN,
            password="pa'ss/#word",
        )
    )

    assert result.status is DatabaseSetupStatus.PLANNED
    assert result.execute is False
    assert result.verification_succeeded is False
    assert result.env_changed is True
    assert result.env_backup_file == str(tmp_path / ".env.bak")
    assert result.connection_url == "mariadb+pymysql://af_test:***@127.0.0.1:3306/af_test"
    assert "pa'ss/#word" not in result.connection_url
    assert runner.calls == []
    assert verifier.calls == []
    assert env_file.read_text(encoding="utf-8") == "AF_OTHER=value\n"


def test_database_setup_execute_preserves_env_creates_backup_and_uses_stdin(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AF_OTHER=value\n", encoding="utf-8")
    backend, runner, verifier = _backend()

    result = backend.setup(
        DatabaseSetupRequest(
            project_root=tmp_path,
            env_file=env_file,
            mode=DatabaseSetupMode.LOCAL_DEBIAN,
            execute=True,
            password="pa'ss/#word",
        )
    )

    assert result.status is DatabaseSetupStatus.APPLIED
    assert result.execute is True
    assert result.verification_succeeded is True
    assert result.env_changed is True
    assert result.env_backup_file == str(tmp_path / ".env.bak")
    assert len(runner.calls) == 4
    assert runner.calls[0].argv == ("sudo", "apt-get", "update")
    assert runner.calls[1].argv == (
        "sudo",
        "apt-get",
        "install",
        "--yes",
        "mariadb-server",
        "mariadb-client",
    )
    assert runner.calls[1].env == {"DEBIAN_FRONTEND": "noninteractive"}
    assert runner.calls[2].argv == ("sudo", "systemctl", "enable", "--now", "mariadb")
    assert runner.calls[3].argv == ("sudo", "mariadb")
    assert runner.calls[3].input_text is not None
    assert "pa''ss/#word" in runner.calls[3].input_text
    assert "password" not in " ".join(" ".join(call.argv) for call in runner.calls)
    assert verifier.calls == ["mariadb+pymysql://af_test:pa%27ss%2F%23word@127.0.0.1:3306/af_test"]
    assert env_file.read_text(encoding="utf-8") == (
        "AF_OTHER=value\n"
        "AF_MARIADB_TEST_URL=mariadb+pymysql://af_test:pa%27ss%2F%23word@127.0.0.1:3306/af_test\n"
    )
    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == "AF_OTHER=value\n"


def test_database_setup_rejects_unsafe_identifiers() -> None:
    with pytest.raises(ValueError, match="database user is invalid"):
        DatabaseSetupRequest(
            project_root=Path("."),
            mode=DatabaseSetupMode.LOCAL_DEBIAN,
            user="af_test'; GRANT ALL PRIVILEGES ON *.* TO root --",
        )

    with pytest.raises(ValueError, match="database name is invalid"):
        DatabaseSetupRequest(
            project_root=Path("."),
            mode=DatabaseSetupMode.LOCAL_DEBIAN,
            database="af_test; DROP DATABASE mysql;",
        )


def test_database_setup_fails_closed_for_remote_host_and_unsupported_platform(
    tmp_path: Path,
) -> None:
    backend, runner, verifier = _backend()
    with pytest.raises(DatabaseSetupError, match="requires a local host"):
        backend.setup(
            DatabaseSetupRequest(
                project_root=tmp_path,
                mode=DatabaseSetupMode.LOCAL_DEBIAN,
                host="db.example.com",
            )
        )

    unsupported_backend, unsupported_runner, unsupported_verifier = _backend(system="Darwin")
    with pytest.raises(DatabaseSetupError, match="only supported on Debian or Ubuntu Linux"):
        unsupported_backend.setup(
            DatabaseSetupRequest(
                project_root=tmp_path,
                mode=DatabaseSetupMode.LOCAL_DEBIAN,
            )
        )
    assert runner.calls == []
    assert verifier.calls == []
    assert unsupported_runner.calls == []
    assert unsupported_verifier.calls == []


def test_database_setup_rerun_rotates_password_and_rewrites_only_target_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AF_OTHER=value\n"
        "AF_MARIADB_TEST_URL=mariadb+pymysql://af_test:old@127.0.0.1:3306/af_test\n",
        encoding="utf-8",
    )
    backend, runner, verifier = _backend()

    first = backend.setup(
        DatabaseSetupRequest(
            project_root=tmp_path,
            env_file=env_file,
            mode=DatabaseSetupMode.LOCAL_DEBIAN,
            execute=True,
            password="new-pass",
        )
    )
    second = backend.setup(
        DatabaseSetupRequest(
            project_root=tmp_path,
            env_file=env_file,
            mode=DatabaseSetupMode.LOCAL_DEBIAN,
            execute=True,
            password="new-pass",
        )
    )

    assert first.env_changed is True
    assert second.env_changed is False
    assert "ALTER USER IF EXISTS 'af_test'@'127.0.0.1' IDENTIFIED BY 'new-pass';" in (
        runner.calls[3].input_text or ""
    )
    assert "ALTER USER IF EXISTS 'af_test'@'127.0.0.1' IDENTIFIED BY 'new-pass';" in (
        runner.calls[7].input_text or ""
    )
    assert verifier.calls == [
        "mariadb+pymysql://af_test:new-pass@127.0.0.1:3306/af_test",
        "mariadb+pymysql://af_test:new-pass@127.0.0.1:3306/af_test",
    ]
    assert env_file.read_text(encoding="utf-8") == (
        "AF_OTHER=value\n"
        "AF_MARIADB_TEST_URL=mariadb+pymysql://af_test:new-pass@127.0.0.1:3306/af_test\n"
    )


def test_database_setup_does_not_mutate_env_when_verification_fails(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AF_OTHER=value\n", encoding="utf-8")
    backend, runner, verifier = _backend(
        verifier=_RecordingVerifier(
            fail=True,
            message="mariadb+pymysql://af_test:very-secret@127.0.0.1:3306/af_test",
        )
    )

    with pytest.raises(DatabaseSetupError, match="connection verification failed") as captured:
        backend.setup(
            DatabaseSetupRequest(
                project_root=tmp_path,
                env_file=env_file,
                mode=DatabaseSetupMode.LOCAL_DEBIAN,
                execute=True,
                password="very-secret",
            )
        )

    assert len(runner.calls) == 4
    assert verifier.calls == ["mariadb+pymysql://af_test:very-secret@127.0.0.1:3306/af_test"]
    assert "very-secret" not in str(captured.value)
    assert env_file.read_text(encoding="utf-8") == "AF_OTHER=value\n"
