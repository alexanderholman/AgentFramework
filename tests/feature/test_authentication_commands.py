"""Local token commands preserve scopes, secrecy and restart/recovery behavior."""

import json
import os
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from af.application.authentication import AuthenticationService
from af.bootstrap import create_application
from af.config import Settings
from af.framework.authentication import AuthenticationDenied
from af.framework.context import Principal
from af.infrastructure.authentication import SQLiteAuthenticationRepository
from af.interfaces.authentication import register_authentication_commands
from af.interfaces.console import create_cli


def token_cli(tmp_path: Path) -> tuple[typer.Typer, AuthenticationService]:
    service = AuthenticationService(SQLiteAuthenticationRepository(tmp_path / "auth.db"))
    cli = typer.Typer()
    register_authentication_commands(cli, lambda _: service, lambda _: "local")
    return cli, service


def issue_arguments(output: Path, scope: str = "af:read") -> list[str]:
    return ["auth", "issue", "--subject", "alice", "--scope", scope, "--output", str(output)]


def test_installed_tokens_rotate_and_revoke_across_application_restarts(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}")
    runner = CliRunner()
    first_path, second_path = tmp_path / "first.json", tmp_path / "second.json"
    first = create_application(settings=settings)
    try:
        result = runner.invoke(create_cli(first), issue_arguments(first_path))
        assert result.exit_code == 0, result.output
        original = json.loads(first_path.read_text())
        assert original["secret"] not in result.output
        assert first_path.stat().st_mode & 0o777 == 0o600
        assert json.loads(result.output)["scopes"] == ["af:read"]
    finally:
        first.shutdown()
    second = create_application(settings=settings)
    try:
        auth = second.container.resolve(AuthenticationService)
        assert auth.authenticate(original["secret"], tenant_id="local").subject_id == "alice"
        result = runner.invoke(
            create_cli(second),
            ["auth", "rotate", "--credential", str(first_path), "--output", str(second_path)],
        )
        assert result.exit_code == 0, result.output
        rotated = json.loads(second_path.read_text())
        assert rotated["secret"] not in result.output
        assert rotated["scopes"] == original["scopes"]
        assert rotated["expires_at"] <= original["expires_at"]
        with pytest.raises(AuthenticationDenied):
            auth.authenticate(original["secret"], tenant_id="local")
    finally:
        second.shutdown()
    third = create_application(settings=settings)
    try:
        auth = third.container.resolve(AuthenticationService)
        assert auth.authenticate(rotated["secret"], tenant_id="local").scopes == ("af:read",)
        with pytest.raises(AuthenticationDenied):
            auth.authenticate(rotated["secret"], tenant_id="other")
        result = runner.invoke(create_cli(third), ["auth", "revoke", rotated["token_id"]])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"revoked": True}
        result = runner.invoke(create_cli(third), ["auth", "revoke", "absent"])
        assert json.loads(result.output) == {"revoked": False}
        with pytest.raises(AuthenticationDenied):
            auth.authenticate(rotated["secret"], tenant_id="local")
    finally:
        third.shutdown()


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_destination_never_rotates_or_overwrites_credentials(
    tmp_path: Path, symlink: bool
) -> None:
    cli, auth = token_cli(tmp_path)
    old = auth.issue(Principal("alice", "user", scopes=("af:read",)), tenant_id="local")
    credential = tmp_path / "old.json"
    credential.write_text(json.dumps({"secret": old.secret}))
    output = tmp_path / "new.json"
    target = tmp_path / "preserved"
    if symlink:
        target.write_text("preserve")
        output.symlink_to(target)
    else:
        output.write_text("preserve")
    result = CliRunner().invoke(
        cli, ["auth", "rotate", "--credential", str(credential), "--output", str(output)]
    )
    assert result.exit_code == 2
    assert json.loads(result.output) == {"error": "token_operation_failed"}
    assert output.read_text() == "preserve"
    assert auth.authenticate(old.secret, tenant_id="local").subject_id == "alice"


@pytest.mark.parametrize(
    "content",
    ["[]", "{}", '{"secret":1}', '{"secret":"a","secret":"b"}', "x" * 4097],
)
def test_bad_credentials_leave_no_destination_and_can_be_retried(
    tmp_path: Path, content: str
) -> None:
    cli, auth = token_cli(tmp_path)
    credential, output = tmp_path / "input.json", tmp_path / "output.json"
    credential.write_text(content)
    args = ["auth", "rotate", "--credential", str(credential), "--output", str(output)]
    failed = CliRunner().invoke(cli, args)
    assert failed.exit_code == 2
    assert json.loads(failed.output) == {"error": "token_operation_failed"}
    assert not output.exists()
    old = auth.issue(Principal("alice", "user", scopes=("af:read",)), tenant_id="local")
    credential.write_text(json.dumps({"secret": old.secret}))
    assert CliRunner().invoke(cli, args).exit_code == 0
    assert auth.authenticate(json.loads(output.read_text())["secret"], tenant_id="local")


def test_duplicate_secret_fields_cannot_revoke_an_existing_token(tmp_path: Path) -> None:
    cli, auth = token_cli(tmp_path)
    old = auth.issue(Principal("alice", "user", scopes=("af:read",)), tenant_id="local")
    credential, output = tmp_path / "input.json", tmp_path / "output.json"
    credential.write_text('{"secret":"different","secret":' + json.dumps(old.secret) + "}")
    failed = CliRunner().invoke(
        cli, ["auth", "rotate", "--credential", str(credential), "--output", str(output)]
    )
    assert failed.exit_code == 2
    assert auth.authenticate(old.secret, tenant_id="local").subject_id == "alice"
    assert not output.exists()


def test_failed_durable_write_revokes_token_removes_partial_file_and_sanitizes_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, auth = token_cli(tmp_path)
    output = tmp_path / "output.json"
    written: dict[str, object] = {}

    def full_disk(descriptor: int) -> None:
        written.update(json.loads(output.read_text()))
        raise OSError(f"sensitive filesystem detail {written['secret']}")

    monkeypatch.setattr(os, "fsync", full_disk)
    failed = CliRunner().invoke(cli, issue_arguments(output))
    assert failed.exit_code == 2
    assert json.loads(failed.output) == {"error": "token_operation_failed"}
    assert not output.exists()
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(str(written["secret"]), tenant_id="local")


def test_revoke_failure_is_still_a_sanitized_transport_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, auth = token_cli(tmp_path)

    def failed_revoke(token_id: str, *, tenant_id: str) -> bool:
        raise OSError("private database credentials")

    monkeypatch.setattr(auth, "revoke", failed_revoke)
    failed = CliRunner().invoke(cli, ["auth", "revoke", "token"])
    assert failed.exit_code == 2
    assert json.loads(failed.output) == {"error": "token_operation_failed"}


def test_invalid_issuance_does_not_leave_empty_file(tmp_path: Path) -> None:
    cli, _ = token_cli(tmp_path)
    output = tmp_path / "output.json"
    failed = CliRunner().invoke(cli, issue_arguments(output, "not a scope"))
    assert failed.exit_code == 2
    assert not output.exists()
    assert CliRunner().invoke(cli, issue_arguments(output)).exit_code == 0


def test_failed_compensation_does_not_leak_secret_or_leave_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, auth = token_cli(tmp_path)
    output = tmp_path / "output.json"

    def full_disk(descriptor: int) -> None:
        raise OSError("private disk error")

    def revoke_unavailable(token_id: str, *, tenant_id: str) -> bool:
        raise OSError("private database error")

    monkeypatch.setattr(os, "fsync", full_disk)
    monkeypatch.setattr(auth, "revoke", revoke_unavailable)
    failed = CliRunner().invoke(cli, issue_arguments(output))
    assert failed.exit_code == 2
    assert json.loads(failed.output) == {"error": "token_operation_failed"}
    assert not output.exists()


def test_cleanup_does_not_delete_a_replacement_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, _ = token_cli(tmp_path)
    output = tmp_path / "output.json"
    replaced = tmp_path / "replaced.json"

    def replaced_during_write(descriptor: int) -> None:
        # Keeping the original inode alive makes this race deterministic.
        output.rename(replaced)
        output.write_text("replacement belongs to another operation")
        raise OSError("write failed")

    monkeypatch.setattr(os, "fsync", replaced_during_write)
    failed = CliRunner().invoke(cli, issue_arguments(output))
    assert failed.exit_code == 2
    assert output.read_text() == "replacement belongs to another operation"


def test_token_help_does_not_resolve_persistent_authentication() -> None:
    def unavailable(context: typer.Context) -> AuthenticationService:
        raise AssertionError("help must not initialize authentication")

    cli = typer.Typer()
    register_authentication_commands(cli, unavailable, lambda _: "local")
    assert CliRunner().invoke(cli, ["auth", "--help"]).exit_code == 0
    assert CliRunner().invoke(cli, ["auth", "issue", "--help"]).exit_code == 0
