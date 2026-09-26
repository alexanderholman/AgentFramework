from fastapi.testclient import TestClient

from af.infrastructure.http_operations import SQLiteHttpOperations
from af.interfaces.http import create_http_app


def test_etag_precondition_rejects_stale_changes_before_execution(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        created = client.post("/api/v1/tasks", json={"prompt": "hello"})
        path = created.headers["location"]
        tag = created.headers["etag"]
        assert client.get(path).headers["etag"] == tag
        changed = client.patch(
            path, json={"prompt": "how many rs in strawberry"}, headers={"if-match": tag}
        )
        assert changed.status_code == 200
        assert changed.headers["etag"] != tag
        before = application.traces.list()
        assert (
            client.patch(path, json={"prompt": "hello"}, headers={"if-match": tag}).status_code
            == 412
        )
        assert client.delete(path, headers={"if-match": tag}).status_code == 412
        assert application.traces.list() == before
        for invalid in ("*", "W/" + tag, tag + ", " + tag, "garbage"):
            assert client.delete(path, headers={"if-match": invalid}).status_code == 400
        assert client.delete(path, headers={"if-match": changed.headers["etag"]}).status_code == 204


def test_installed_idempotency_replays_exact_response_and_fences_policy_changes(
    application, tmp_path
):
    ledger = SQLiteHttpOperations(tmp_path / "api.db")
    with TestClient(
        create_http_app(application, http_operations=ledger), base_url="http://127.0.0.1"
    ) as client:
        headers = {"idempotency-key": "create-1"}
        first = client.post("/api/v1/tasks", json={"prompt": "hello"}, headers=headers)
        replay = client.post("/api/v1/tasks", json={"prompt": "hello"}, headers=headers)
        assert first.status_code == replay.status_code == 201
        assert first.content == replay.content
        assert replay.headers["etag"] == first.headers["etag"]
        assert replay.headers["idempotency-replayed"] == "true"
        assert len(application.tasks.list()) == 1
        assert (
            client.post(
                "/api/v1/tasks", json={"prompt": "how many rs in strawberry"}, headers=headers
            ).status_code
            == 409
        )
    ledger.close()
    reopened = SQLiteHttpOperations(tmp_path / "api.db")
    with TestClient(
        create_http_app(application, http_operations=reopened), base_url="http://127.0.0.1"
    ) as client:
        assert (
            client.post("/api/v1/tasks", json={"prompt": "hello"}, headers=headers).content
            == first.content
        )
    with TestClient(
        create_http_app(application, http_operations=reopened, api_policy_version="new"),
        base_url="http://127.0.0.1",
    ) as client:
        assert (
            client.post("/api/v1/tasks", json={"prompt": "hello"}, headers=headers).status_code
            == 409
        )
    reopened.close()


def test_api_rate_limit_and_invalid_keys_do_not_execute(application, tmp_path):
    ledger = SQLiteHttpOperations(tmp_path / "api.db", requests_per_window=2)
    with TestClient(
        create_http_app(application, http_operations=ledger), base_url="http://127.0.0.1"
    ) as client:
        assert client.get("/api/v1/tasks", headers={"idempotency-key": "read"}).status_code == 400
        assert (
            client.post(
                "/api/v1/tasks", json={"prompt": "hello"}, headers={"idempotency-key": "bad key"}
            ).status_code
            == 400
        )
        limited = client.post("/api/v1/tasks", json={"prompt": "hello"})
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0
        assert application.tasks.list() == []
    ledger.close()


def test_default_persistent_ledger_survives_real_application_restart(tmp_path):
    from af.bootstrap import create_application
    from af.config import Settings

    settings = Settings(database_url=f"sqlite+pysqlite:///{tmp_path / 'tasks.db'}")
    first = create_application(settings=settings)
    try:
        with TestClient(create_http_app(first), base_url="http://127.0.0.1") as client:
            result = client.post(
                "/api/v1/tasks", json={"prompt": "hello"}, headers={"idempotency-key": "durable"}
            )
            assert result.status_code == 201
    finally:
        first.shutdown()
    second = create_application(settings=settings)
    try:
        with TestClient(create_http_app(second), base_url="http://127.0.0.1") as client:
            replay = client.post(
                "/api/v1/tasks", json={"prompt": "hello"}, headers={"idempotency-key": "durable"}
            )
            assert replay.content == result.content
            assert replay.headers["idempotency-replayed"] == "true"
            assert len(second.tasks.list()) == 1
    finally:
        second.shutdown()


def test_incomplete_or_server_error_requests_are_never_automatically_reexecuted(tmp_path):
    from fastapi import FastAPI
    from starlette.responses import Response

    from af.interfaces.http_operations import HttpOperationsMiddleware

    ledger = SQLiteHttpOperations(tmp_path / "uncertain.db")
    calls = []
    app = FastAPI()

    @app.post("/api/v1/tasks")
    def operation():
        calls.append("side effect")
        return Response("private failure", status_code=500)

    app.add_middleware(HttpOperationsMiddleware, operations=ledger)
    with TestClient(app) as client:
        assert (
            client.post("/api/v1/tasks", headers={"idempotency-key": "uncertain"}).status_code
            == 500
        )
        second = client.post("/api/v1/tasks", headers={"idempotency-key": "uncertain"})
        assert second.status_code == 409
        assert calls == ["side effect"]
    ledger.close()


def test_principal_tenant_and_scope_changes_cannot_replay_another_authority(tmp_path):
    from fastapi import FastAPI

    from af.framework.context import ExecutionContext, Principal, use_context
    from af.interfaces.http_operations import HttpOperationsMiddleware

    ledger = SQLiteHttpOperations(tmp_path / "identities.db")
    calls = []
    inner = FastAPI()

    @inner.post("/api/v1/tasks")
    def operation():
        calls.append("executed")
        return {"id": len(calls)}

    middleware = HttpOperationsMiddleware(inner, operations=ledger)
    context = ExecutionContext(
        principal=Principal("one", "user", scopes=("write",)), tenant_id="tenant-a"
    )

    async def authenticated(scope, receive, send):
        with use_context(context):
            await middleware(scope, receive, send)

    with TestClient(authenticated) as client:
        headers = {"idempotency-key": "shared"}
        assert client.post("/api/v1/tasks", headers=headers).json() == {"id": 1}
        assert client.post("/api/v1/tasks", headers=headers).json() == {"id": 1}
        context = ExecutionContext(
            principal=Principal("one", "user", scopes=("read",)), tenant_id="tenant-a"
        )
        assert client.post("/api/v1/tasks", headers=headers).status_code == 409
        context = ExecutionContext(
            principal=Principal("two", "user", scopes=("write",)), tenant_id="tenant-a"
        )
        assert client.post("/api/v1/tasks", headers=headers).json() == {"id": 2}
        context = ExecutionContext(
            principal=Principal("one", "user", scopes=("write",)), tenant_id="tenant-b"
        )
        assert client.post("/api/v1/tasks", headers=headers).json() == {"id": 3}
    ledger.close()


def test_idempotency_replay_reauthenticates_and_denies_revoked_credentials(tmp_path):
    from af.application.authentication import AuthenticationService
    from af.bootstrap import create_application
    from af.config import Settings
    from af.framework.context import Principal

    app = create_application(
        settings=Settings(database_url=f"sqlite:///{tmp_path / 'auth.db'}", http_auth_required=True)
    )
    try:
        auth = app.container.resolve(AuthenticationService)
        writer = auth.issue(
            Principal("writer", "user", scopes=("af:read", "af:write")), tenant_id="local"
        )
        headers = {"authorization": "Bearer " + writer.secret, "idempotency-key": "private-replay"}
        with TestClient(create_http_app(app), base_url="http://localhost") as client:
            assert (
                client.post("/api/v1/tasks", json={"prompt": "hello"}, headers=headers).status_code
                == 201
            )
            assert (
                client.post("/api/v1/tasks", json={"prompt": "hello"}, headers=headers).headers[
                    "idempotency-replayed"
                ]
                == "true"
            )
            auth.revoke(writer.token_id, tenant_id="local")
            assert (
                client.post("/api/v1/tasks", json={"prompt": "hello"}, headers=headers).status_code
                == 401
            )
            assert (
                client.post(
                    "/api/v1/tasks",
                    json={"prompt": "hello"},
                    headers={"idempotency-key": "private-replay"},
                ).status_code
                == 401
            )
    finally:
        app.shutdown()


def test_json_and_xml_variants_both_declare_vary_accept(application):
    with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
        for accept in ("application/json", "application/xml"):
            response = client.get("/api/v1/tasks", headers={"accept": accept})
            assert "Accept" in response.headers["vary"]
