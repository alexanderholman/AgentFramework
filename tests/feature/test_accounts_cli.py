"""Local encrypted notification export completes a headless account journey."""

import json
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from af.application.account_controller import AccountController
from af.application.accounts import AccountService
from af.application.authentication import AuthenticationService
from af.framework.authentication import AuthenticationDenied
from af.infrastructure.account_notifications import SQLiteAccountNotificationOutbox
from af.infrastructure.accounts import FernetSecretCipher, SQLiteAccountRepository
from af.interfaces.accounts import register_account_commands


class FakeHasher:
    """Account workflow tests isolate cryptographic cost; scrypt has a real integration test."""

    def hash(self, password: str) -> str:
        return "hash:" + password

    def verify(self, password: str, digest: str) -> bool:
        return digest == self.hash(password)


def test_account_cli_local_outbox_never_echoes_secrets(tmp_path: Path) -> None:
    repo = SQLiteAccountRepository(tmp_path / "db")
    auth = AuthenticationService(repo)
    cipher = FernetSecretCipher.generate()
    outbox = SQLiteAccountNotificationOutbox(tmp_path / "db", cipher, tenant_id="a")
    accounts = AccountService(repo, auth, FakeHasher(), cipher, outbox)
    controller = AccountController(accounts, auth, outbox, tenant_id="a")
    cli = typer.Typer()
    register_account_commands(cli, lambda ctx: controller)
    runner = CliRunner()
    number = 0

    def run(action: str, payload: dict[str, object]) -> dict[str, object]:
        nonlocal number
        number += 1
        request, output = tmp_path / f"request{number}", tmp_path / f"output{number}"
        request.write_text(json.dumps(payload))
        result = runner.invoke(
            cli, ["accounts", action, "--request", str(request), "--output", str(output)]
        )
        assert result.exit_code == 0, result.output
        assert output.stat().st_mode & 0o777 == 0o600
        assert "secret" not in result.output
        return json.loads(output.read_text())

    password = "a sufficiently long password"
    run("register", {"address": "alice@example.com", "password": password})
    run("verification-request", {"address": "alice@example.com"})
    notices = run("notifications", {})["notifications"]
    assert isinstance(notices, list)
    message = next(x for x in notices if x["message_id"] == outbox.pending()[-1].message_id)
    content = run("notification-export", {"message_id": message["message_id"]})
    assert content["secret"].encode() not in (tmp_path / "db").read_bytes()
    run("verify", {"secret": content["secret"]})
    token = run("login", {"address": "alice@example.com", "password": password, "factor": None})
    assert auth.authenticate(str(token["secret"]), tenant_id="a")
    run("reset-request", {"address": "alice@example.com"})
    reset_id = next(item for item in outbox.pending() if item.kind == "reset").message_id
    reset = run("notification-export", {"message_id": reset_id})
    run("reset", {"secret": reset["secret"], "password": "a new sufficiently long password"})
    with pytest.raises(AuthenticationDenied):
        auth.authenticate(str(token["secret"]), tenant_id="a")
    request, output = tmp_path / "bad", tmp_path / "existing"
    request.write_text('{"address":"alice@example.com","password":"x","factor":null}')
    output.write_text("preserve")
    assert (
        runner.invoke(
            cli, ["accounts", "login", "--request", str(request), "--output", str(output)]
        ).exit_code
        == 2
    )
    assert output.read_text() == "preserve"
    output.unlink()
    assert (
        runner.invoke(
            cli, ["accounts", "login", "--request", str(request), "--output", str(output)]
        ).exit_code
        == 2
    )
    assert not output.exists()
