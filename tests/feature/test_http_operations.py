from fastapi.testclient import TestClient

from af.interfaces.http import create_http_app
from af.infrastructure.http_operations import SQLiteHttpOperations


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
