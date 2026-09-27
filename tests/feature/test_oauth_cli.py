"""Headless CLI grants write credentials privately and never echo their secrets."""

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from af.application.authentication import AuthenticationService, OAuthAuthorizationService
from af.application.oauth_controller import OAuthController
from af.application.oauth_grants import OAuthGrantService
from af.framework.context import Principal
from af.infrastructure.oauth_grants import SQLiteOAuthGrantRepository
from af.interfaces.oauth_grants import register_oauth_commands


def test_oauth_cli_service_refresh_and_device(tmp_path: Path) -> None:
    repo = SQLiteOAuthGrantRepository(tmp_path / "db")
    auth = AuthenticationService(repo)
    grants = OAuthGrantService(repo, auth)
    controller = OAuthController(grants, OAuthAuthorizationService(repo, auth), auth, "local")
    cli = typer.Typer()
    register_oauth_commands(cli, lambda ctx: controller)
    runner = CliRunner()
    counter = 0

    def run(action: str, data: dict[str, object]) -> dict[str, object]:
        nonlocal counter
        counter += 1
        request, output = tmp_path / f"request{counter}", tmp_path / f"output{counter}"
        request.write_text(json.dumps(data))
        result = runner.invoke(
            cli, ["oauth", action, "--request", str(request), "--output", str(output)]
        )
        assert result.exit_code == 0, result.output
        returned = json.loads(output.read_text())
        assert output.stat().st_mode & 0o777 == 0o600
        assert "secret" not in result.output
        return returned

    credential = run("service-register", {"client_id": "worker", "scopes": ["af:read"]})
    token = run("client-credentials", {**credential, "scopes": ["af:read"]})
    assert auth.authenticate(str(token["secret"]), tenant_id="local").subject_id == "worker"
    run("service-revoke", {"client_id": "worker"})
    run(
        "client-register",
        {"client_id": "desktop", "redirect_uris": ["https://af.example/cb"], "scopes": ["af:read"]},
    )
    personal = auth.issue(Principal("alice", "user", scopes=("af:read",)), tenant_id="local")
    pair = run(
        "refresh-issue",
        {
            "client_id": "desktop",
            "principal_secret": personal.secret,
            "scopes": ["af:read"],
            "consented": True,
        },
    )
    renewed = run(
        "refresh",
        {"client_id": "desktop", "refresh_secret": pair["refresh_secret"], "scopes": ["af:read"]},
    )
    run("refresh-revoke", {"client_id": "desktop", "refresh_secret": renewed["refresh_secret"]})
    device = run(
        "device-start",
        {
            "client_id": "desktop",
            "scopes": ["af:read"],
            "verification_uri": "https://af.example/device",
        },
    )
    run(
        "device-approve",
        {"user_code": device["user_code"], "principal_secret": personal.secret, "consented": True},
    )
    run("device-poll", {"client_id": "desktop", "device_code": device["device_code"]})
    run("client-revoke", {"client_id": "desktop"})


def test_oauth_cli_errors_do_not_issue_or_overwrite_credentials(tmp_path: Path) -> None:
    repo = SQLiteOAuthGrantRepository(tmp_path / "db")
    auth = AuthenticationService(repo)
    controller = OAuthController(
        OAuthGrantService(repo, auth), OAuthAuthorizationService(repo, auth), auth, "local"
    )
    cli = typer.Typer()
    register_oauth_commands(cli, lambda ctx: controller)
    runner = CliRunner()
    request, output = tmp_path / "request", tmp_path / "output"
    output.write_text("preserve")
    request.write_text(json.dumps({"client_id": "worker", "scopes": ["af:read"]}))
    args = ["oauth", "service-register", "--request", str(request), "--output", str(output)]
    assert runner.invoke(cli, args).exit_code == 2
    assert output.read_text() == "preserve"
    assert repo.get_service_client("worker", "local") is None
    output.unlink()
    for malformed in (
        '{"client_id":"a","client_id":"b"}',
        "[0]",
        '{"scopes":true}',
        '{"client_id":1,"scopes":[]}',
        '{"client_id":"w","scopes":[1]}',
    ):
        request.write_text(malformed)
        assert runner.invoke(cli, args).exit_code == 2
        assert not output.exists()
    personal = auth.issue(Principal("alice", "user", scopes=("read",)), tenant_id="local")
    for action, payload in (
        (
            "refresh-issue",
            {
                "client_id": "web",
                "principal_secret": personal.secret,
                "scopes": ["read"],
                "consented": False,
            },
        ),
        (
            "device-approve",
            {"user_code": "unknown", "principal_secret": personal.secret, "consented": "yes"},
        ),
        ("device-poll", {"client_id": "web", "device_code": "unknown"}),
    ):
        request.write_text(json.dumps(payload))
        failed = runner.invoke(
            cli, ["oauth", action, "--request", str(request), "--output", str(output)]
        )
        assert failed.exit_code == 2
        assert personal.secret not in failed.output
        assert not output.exists()
