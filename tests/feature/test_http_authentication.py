from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.application.authentication import AuthenticationService
from af.bootstrap import create_application
from af.config import Settings
from af.framework.context import Principal
from af.framework.errors import InvalidProviderConfiguration
from af.interfaces.console import create_cli
from af.interfaces.http import create_http_app
from af.interfaces.mcp import create_mcp_server


def test_allowed_hosts_do_not_authorize_unauthenticated_network_listeners(tmp_path: Path) -> None:
    application = create_application(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'state.db'}",
            host="0.0.0.0",
            allowed_hosts=("af.internal",),
            allowed_origins=("https://af.internal",),
            mcp_transport="streamable-http",
        )
    )
    try:
        with pytest.raises(InvalidProviderConfiguration, match="authentication"):
            create_http_app(application)
        with pytest.raises(InvalidProviderConfiguration, match="loopback"):
            create_mcp_server(application)
    finally:
        application.shutdown()


def test_nonloopback_http_requires_authentication_and_private_session_key(tmp_path: Path) -> None:
    app = create_application(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'state.db'}",
            host="0.0.0.0",
            allowed_hosts=("af.internal",),
            http_auth_required=True,
        )
    )
    try:
        with pytest.raises(InvalidProviderConfiguration, match="session"):
            create_http_app(app)
    finally:
        app.shutdown()
    app = create_application(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'state.db'}",
            host="0.0.0.0",
            allowed_hosts=("af.internal",),
            http_auth_required=True,
            session_cookie_signing_key="private-host-specific-signing-key-0123456789",
        )
    )
    try:
        token = app.container.resolve(AuthenticationService).issue(
            Principal("operator", "user", scopes=("af:read",)), tenant_id="local"
        )
        with TestClient(create_http_app(app), base_url="http://af.internal") as client:
            assert client.get("/api/v1/tasks").status_code == 401
            assert (
                client.get(
                    "/api/v1/tasks",
                    headers={
                        "Authorization": "Bearer " + token.secret,
                    },
                ).status_code
                == 200
            )
    finally:
        app.shutdown()


def test_http_bearer_scope_revocation_and_tenant_binding(tmp_path: Path) -> None:
    application = create_application(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'state.db'}", http_auth_required=True
        )
    )
    try:
        authentication = application.container.resolve(AuthenticationService)
        reader = authentication.issue(
            Principal("reader", "user", scopes=("af:read",)), tenant_id="local"
        )
        writer = authentication.issue(
            Principal("writer", "user", scopes=("af:read", "af:write")), tenant_id="local"
        )
        other = authentication.issue(
            Principal("writer", "user", scopes=("af:read", "af:write")), tenant_id="other"
        )
        with TestClient(create_http_app(application), base_url="http://localhost") as client:
            assert client.get("/api/v1/tasks").status_code == 401
            assert (
                client.get(
                    "/api/v1/tasks", headers={"Authorization": "Bearer " + other.secret}
                ).status_code
                == 401
            )
            assert (
                client.get(
                    "/api/v1/tasks", headers={"Authorization": "Bearer " + reader.secret}
                ).status_code
                == 200
            )
            assert (
                client.post(
                    "/api/v1/tasks",
                    json={"prompt": "hello"},
                    headers={"Authorization": "Bearer " + reader.secret},
                ).status_code
                == 401
            )
            created = client.post(
                "/api/v1/tasks",
                json={"prompt": "hello"},
                headers={"Authorization": "Bearer " + writer.secret},
            )
            assert created.status_code == 201
            authentication.revoke(writer.token_id, tenant_id="local")
            assert (
                client.get(
                    "/api/v1/tasks", headers={"Authorization": "Bearer " + writer.secret}
                ).status_code
                == 401
            )
    finally:
        application.shutdown()


def test_token_cli_outputs_private_file_and_can_revoke(tmp_path: Path) -> None:
    import json

    application = create_application(
        settings=Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}")
    )
    try:
        runner = CliRunner()
        cli = create_cli(application)
        output = tmp_path / "credential.json"
        result = runner.invoke(
            cli,
            [
                "auth",
                "issue",
                "--subject",
                "local-worker",
                "--scope",
                "af:read",
                "--output",
                str(output),
            ],
        )
        assert result.exit_code == 0, result.output
        credential = json.loads(output.read_text())
        assert credential["secret"] not in result.output
        assert output.stat().st_mode & 0o077 == 0
        result = runner.invoke(cli, ["auth", "revoke", credential["token_id"]])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["revoked"] is True
    finally:
        application.shutdown()
