from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from af.application.authentication import AuthenticationService
from af.bootstrap import create_application
from af.config import Settings
from af.framework.authentication import AuthenticationDenied
from af.interfaces.console import create_cli


def test_accounts_recover_across_installed_application_restarts(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}")
    runner = CliRunner()
    request = tmp_path / "request.json"
    password = "first long private account password"
    address = "alice@example.com"
    index = 0

    def run(action: str, data: dict[str, object]) -> dict[str, object]:
        nonlocal index
        index += 1
        request.write_text(json.dumps(data))
        output = tmp_path / f"output-{index}.json"
        result = runner.invoke(
            create_cli(app),
            ["accounts", action, "--request", str(request), "--output", str(output)],
        )
        assert result.exit_code == 0, result.output
        assert output.stat().st_mode & 0o077 == 0
        payload = json.loads(output.read_text())
        for name in ("secret", "password", "totp_secret"):
            if name in payload:
                assert payload[name] not in result.output
        return payload

    app = create_application(settings=settings)
    try:
        run("register", {"address": address, "password": password})
        pending = run("notifications", {})["notifications"]
        assert isinstance(pending, list) and len(pending) == 1
        message_id = pending[0]["message_id"]
    finally:
        app.shutdown()

    app = create_application(settings=settings)
    try:
        # The encrypted outbox and its host-derived encryption key survive restart.
        exported = run("notification-export", {"message_id": message_id})
        run("verify", {"secret": exported["secret"]})
        issued = run("login", {"address": address, "password": password, "factor": None})
        secret = str(issued["secret"])
        auth = app.container.resolve(AuthenticationService)
        principal = auth.authenticate(secret, tenant_id="local")
        assert set(principal.scopes) == {"af:read", "af:write"}
        run("reset-request", {"address": address})
        pending = run("notifications", {})["notifications"]
        assert isinstance(pending, list)
        reset_id = next(item["message_id"] for item in pending if item["kind"] == "reset")
    finally:
        app.shutdown()

    app = create_application(settings=settings)
    try:
        exported = run("notification-export", {"message_id": reset_id})
        new_password = "replacement long private account password"
        run("reset", {"secret": exported["secret"], "password": new_password})
        with pytest.raises(AuthenticationDenied):
            app.container.resolve(AuthenticationService).authenticate(secret, tenant_id="local")
        issued = run("login", {"address": address, "password": new_password, "factor": None})
        assert issued["secret"] != secret
        # Passkeys cannot silently select a relying-party origin.
        request.write_text(json.dumps({"address": address}))
        absent = runner.invoke(
            create_cli(app),
            [
                "accounts",
                "passkey-login-begin",
                "--request",
                str(request),
                "--output",
                str(tmp_path / "disabled-passkey.json"),
            ],
        )
        assert absent.exit_code == 2
        assert not (tmp_path / "disabled-passkey.json").exists()
    finally:
        app.shutdown()


def test_passkey_configuration_requires_pair() -> None:
    with pytest.raises(ValueError, match="both"):
        Settings(passkey_rp_id="example.com")
    with pytest.raises(ValueError, match="both"):
        Settings(passkey_origin="https://example.com")


def test_configured_passkey_options_use_pinned_origin(tmp_path: Path) -> None:
    app = create_application(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'accounts.db'}",
            passkey_rp_id="example.com",
            passkey_origin="https://example.com",
        )
    )
    try:
        # The opt-in binding can be resolved independently of any online provider.
        from af.application.account_controller import AccountController

        controller = app.container.resolve(AccountController)
        controller.run(
            "register",
            {
                "address": "alice@example.com",
                "password": "long enough private password",
            },
        )
        notifications = controller.run("notifications", {})["notifications"]
        assert isinstance(notifications, list)
        exported = controller.run(
            "notification-export",
            {
                "message_id": notifications[0]["message_id"],
            },
        )
        controller.run("verify", {"secret": exported["secret"]})
        issued = controller.run(
            "login",
            {
                "address": "alice@example.com",
                "password": "long enough private password",
                "factor": None,
            },
        )
        ceremony = controller.run(
            "passkey-register-begin",
            {
                "principal_secret": issued["secret"],
                "password": "long enough private password",
                "factor": None,
            },
        )
        options = json.loads(str(ceremony["options_json"]))
        assert options["rp"]["id"] == "example.com"
        assert options["authenticatorSelection"]["userVerification"] == "required"
    finally:
        app.shutdown()
