from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import af.interfaces.http as http_module
from af.bootstrap import AFApplication, create_application
from af.config import Settings
from af.domain.tasks import Task
from af.framework.application import ApplicationState
from af.framework.container import Container, ProviderRegistrar, Scope, ServiceProvider
from af.framework.context import maybe_current_context
from af.framework.errors import InvalidPrompt, InvalidProviderConfiguration
from af.framework.types import PROMPT_MAX_LENGTH
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository
from af.interfaces.graphql import create_graphql_router
from af.interfaces.http import create_http_app
from af.interfaces.http_body import HTTP_MAX_BODY_BYTES

CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')
FLASH_PATTERN = re.compile(r'<p role="status">([^<]+)</p>')
TEST_HOST = "127.0.0.1"
SESSION_COOKIE_NAME = "af-session"


def _extract_csrf_token(html: str) -> str:
    match = CSRF_PATTERN.search(html)
    assert match is not None
    return match.group(1)


def _extract_flash_message(html: str) -> str | None:
    match = FLASH_PATTERN.search(html)
    return None if match is None else match.group(1)


def _sign_session_cookie(session: dict[str, str], *, key: str) -> str:
    payload = json.dumps(session, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(key.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"{encoded}.{signature}"


def _session_cookie_was_cleared(response) -> bool:  # type: ignore[no-untyped-def]
    return any(
        SESSION_COOKIE_NAME in value and "Max-Age=0" in value
        for value in response.headers.get_list("set-cookie")
    )


def _graphql_client(controller: object) -> TestClient:
    app = FastAPI()
    app.include_router(create_graphql_router(controller), prefix="/graphql")
    return TestClient(app, base_url=f"http://{TEST_HOST}")


def _http_client(
    application: AFApplication,
    *,
    scheme: str = "http",
    base_url: str | None = None,
) -> TestClient:
    return TestClient(create_http_app(application), base_url=base_url or f"{scheme}://{TEST_HOST}")


def _task(prompt: str, *, task_id: str = "task-1") -> Task:
    now = datetime.now(UTC)
    return Task(
        id=task_id,
        prompt=prompt,
        output="3",
        intent="text.character_count",
        route="character-count",
        tier="procedural",
        status="completed",
        verified=True,
        reused=False,
        trace_id=f"trace-{task_id}",
        created_at=now,
        updated_at=now,
    )


def test_html_index_and_form_answer(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        index = client.get("/")
        csrf_token = _extract_csrf_token(index.text)
        created = client.post(
            "/tasks",
            data={"prompt": "how many rs in strawberry", "csrf_token": csrf_token},
            follow_redirects=True,
        )

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "AF Agent Framework" in index.text
    assert created.status_code == 200
    assert 'data-testid="answer">3<' in created.text


def test_http_web_profiles_require_explicit_experimental_opt_in(
    application,
) -> None:  # type: ignore[no-untyped-def]
    with TestClient(
        create_http_app(application, web_profile="basic"),
        base_url=f"http://{TEST_HOST}",
    ) as basic_client:
        basic = basic_client.get("/")

    with TestClient(
        create_http_app(application, web_profile="pretty"),
        base_url=f"http://{TEST_HOST}",
    ) as pretty_client:
        pretty = pretty_client.get("/")

    with TestClient(
        create_http_app(application, web_profile="experimental"),
        base_url=f"http://{TEST_HOST}",
    ) as fallback_client:
        fallback = fallback_client.get("/")

    with TestClient(
        create_http_app(
            application,
            web_profile="experimental",
            enable_experimental_web_features=True,
        ),
        base_url=f"http://{TEST_HOST}",
    ) as experimental_client:
        experimental = experimental_client.get("/")

    assert 'data-af-web-profile="basic"' in basic.text
    assert 'data-af-web-profile="pretty"' in pretty.text
    assert 'data-af-web-profile="pretty"' in fallback.text
    assert 'data-af-experimental="fallback"' in fallback.text
    assert 'data-af-web-profile="experimental"' in experimental.text
    assert 'data-af-experimental="enabled"' in experimental.text


def test_http_rejects_unknown_web_profile_with_allowed_values() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    with pytest.raises(
        InvalidProviderConfiguration,
        match=(
            r"web profile must be one of basic, experimental, pretty; "
            r"got 'retro-terminal'"
        ),
    ):
        create_http_app(application, web_profile="retro-terminal")


def test_http_request_context_is_scoped_per_invocation() -> None:
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    observed: list[tuple[str | None, str | None, str | None]] = []
    original_list = application.tasks.list

    def record_list(*, limit=None, offset=0):  # type: ignore[no-untyped-def]
        context = maybe_current_context()
        observed.append(
            (
                None if context is None else context.correlation_id,
                None
                if context is None
                else None
                if context.effective_scope is None
                else context.effective_scope.value,
                None
                if context is None
                else None
                if context.requested_scope is None
                else context.requested_scope.value,
            )
        )
        return original_list(limit=limit, offset=offset)

    application.tasks.list = record_list  # type: ignore[assignment]

    with _http_client(application) as client:
        first = client.get("/tasks")
        second = client.get("/tasks")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(observed) == 2
    assert observed[0][0] is not None
    assert observed[1][0] is not None
    assert observed[0][0] != observed[1][0]
    assert observed[0][1:] == [("deterministic", "deterministic")][0]
    assert observed[1][1:] == [("deterministic", "deterministic")][0]


def test_http_creates_and_disposes_request_invocation_scope() -> None:
    class RequestScopedProbe:
        def __init__(self) -> None:
            self.disposed = False

        def close(self) -> None:
            self.disposed = True

    class Provider(ServiceProvider):
        provider_id = "test.http-request-scope"

        def register(self, container: ProviderRegistrar) -> None:
            container.bind(
                RequestScopedProbe,
                lambda _: RequestScopedProbe(),
                scope=Scope.INVOCATION,
            )

    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(Provider(),),
    )
    app = create_http_app(application)
    observed_probes: list[RequestScopedProbe] = []

    @app.get("/_request-scope")
    async def request_scope_probe(request: Request) -> dict[str, object]:
        invocation = request.state.af_invocation
        probe = invocation.resolve(RequestScopedProbe)
        observed_probes.append(probe)
        context = invocation.context
        return {
            "probe_id": id(probe),
            "disposed": probe.disposed,
            "correlation_id": context.correlation_id,
            "effective_scope": None
            if context.effective_scope is None
            else context.effective_scope.value,
            "requested_scope": None
            if context.requested_scope is None
            else context.requested_scope.value,
            "kind": invocation.invocation_kind.value,
        }

    with TestClient(
        app,
        base_url=f"http://{TEST_HOST}",
        raise_server_exceptions=False,
    ) as client:
        first = client.get("/_request-scope")
        second = client.get("/_request-scope")

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["disposed"] is False
    assert second_payload["disposed"] is False
    assert first_payload["kind"] == "request"
    assert second_payload["kind"] == "request"
    assert first_payload["effective_scope"] == "deterministic"
    assert second_payload["effective_scope"] == "deterministic"
    assert first_payload["requested_scope"] == "deterministic"
    assert second_payload["requested_scope"] == "deterministic"
    assert first_payload["probe_id"] != second_payload["probe_id"]
    assert first_payload["correlation_id"] != second_payload["correlation_id"]
    assert len(observed_probes) == 2
    assert observed_probes[0] is not observed_probes[1]
    assert all(probe.disposed for probe in observed_probes)


def test_http_app_exposes_the_composed_ideon_route(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        response = client.post(
            "/api/v1/ideons/interpret",
            json={
                "surface": "sat on the bank",
                "source_id": "fixture:http-ideon",
                "rights": "internal-test-fixture",
            },
        )

    assert response.status_code == 200
    assert response.json()["decision"]["decision"] == "ask"


def test_html_task_lifecycle(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        empty_list = client.get("/tasks")
        index = client.get("/")
        create_token = _extract_csrf_token(index.text)
        create_response = client.post(
            "/tasks",
            data={"prompt": "how many rs in strawberry", "csrf_token": create_token},
            follow_redirects=False,
        )
        task_location = create_response.headers["location"]

        populated_list = client.get("/tasks")
        shown = client.get(task_location)
        list_after_create = client.get("/tasks")
        update_token = _extract_csrf_token(shown.text)
        update_response = client.post(
            task_location,
            data={"prompt": "how many bs in blueberry", "csrf_token": update_token},
            follow_redirects=False,
        )
        updated = client.get(task_location)
        shown_after_update = client.get(task_location)
        delete_token = _extract_csrf_token(updated.text)
        delete_response = client.post(
            f"{task_location}/delete",
            data={"csrf_token": delete_token},
            follow_redirects=False,
        )
        deleted_list = client.get("/tasks")
        list_after_delete = client.get("/tasks")
        missing = client.get(task_location)

    assert empty_list.status_code == 200
    assert "No persisted tasks yet." in empty_list.text
    assert create_response.status_code == 303
    assert task_location.startswith("/tasks/")
    assert "how many rs in strawberry" in populated_list.text
    assert "→ 3" in populated_list.text
    assert _extract_flash_message(populated_list.text) == "Saved task."
    assert _session_cookie_was_cleared(populated_list)
    assert _extract_flash_message(list_after_create.text) is None
    assert shown.status_code == 200
    assert 'data-testid="answer">3<' in shown.text
    assert 'maxlength="10000"' in shown.text
    assert update_response.status_code == 303
    assert update_response.headers["location"] == task_location
    assert "how many bs in blueberry" in updated.text
    assert 'data-testid="answer">2<' in updated.text
    assert _extract_flash_message(updated.text) == "Updated task."
    assert _session_cookie_was_cleared(updated)
    assert _extract_flash_message(shown_after_update.text) is None
    assert delete_response.status_code == 303
    assert delete_response.headers["location"] == "/tasks"
    assert "No persisted tasks yet." in deleted_list.text
    assert _extract_flash_message(deleted_list.text) == "Deleted task."
    assert _session_cookie_was_cleared(deleted_list)
    assert _extract_flash_message(list_after_delete.text) is None
    assert missing.status_code == 404
    assert missing.json() == {"detail": f"task not found: {task_location.removeprefix('/tasks/')}"}


def test_html_flash_cookie_round_trips_across_application_instances_with_shared_key() -> None:
    settings = Settings(session_cookie_signing_key="shared-worker-key")
    task_repository = InMemoryTaskRepository()
    trace_repository = InMemoryTraceRepository()
    first_application = create_application(
        settings=settings,
        task_repository=task_repository,
        trace_repository=trace_repository,
    )
    second_application = create_application(
        settings=settings,
        task_repository=task_repository,
        trace_repository=trace_repository,
    )

    try:
        with _http_client(first_application) as first_client, _http_client(
            second_application
        ) as second_client:
            index = first_client.get("/")
            created = first_client.post(
                "/tasks",
                data={
                    "prompt": "how many rs in strawberry",
                    "csrf_token": _extract_csrf_token(index.text),
                },
                follow_redirects=False,
            )
            task_location = created.headers["location"]
            second_client.cookies.update(first_client.cookies)
            shown = second_client.get(task_location)
            replay = second_client.get(task_location)

        assert shown.status_code == 200
        assert _extract_flash_message(shown.text) == "Saved task."
        assert _session_cookie_was_cleared(shown)
        assert _extract_flash_message(replay.text) is None
    finally:
        first_application.shutdown()
        second_application.shutdown()


@pytest.mark.parametrize(
    "cookie_value",
    [
        "malformed",
        _sign_session_cookie({"flash": "Saved task."}, key="wrong-signing-key"),
        _sign_session_cookie({"notice": "stale"}, key="html-session-key"),
        f"{'A' * 5000}.deadbeef",
    ],
)
def test_html_clears_malformed_invalid_and_obsolete_session_cookies(cookie_value: str) -> None:
    application = create_application(
        settings=Settings(session_cookie_signing_key="html-session-key"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    try:
        with _http_client(application) as client:
            response = client.get("/tasks", cookies={SESSION_COOKIE_NAME: cookie_value})

        assert response.status_code == 200
        assert _extract_flash_message(response.text) is None
        assert _session_cookie_was_cleared(response)
    finally:
        application.shutdown()


def test_html_rejects_blank_create_with_html_feedback(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        index = client.get("/")
        response = client.post(
            "/tasks",
            data={"prompt": "   ", "csrf_token": _extract_csrf_token(index.text)},
        )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "prompt must not be blank" in response.text
    assert application.tasks.list() == []


def test_html_rejects_too_long_update_with_html_feedback(application) -> None:  # type: ignore[no-untyped-def]
    too_long = "a" * 10_001

    with _http_client(application) as client:
        index = client.get("/")
        created = client.post(
            "/tasks",
            data={
                "prompt": "how many rs in strawberry",
                "csrf_token": _extract_csrf_token(index.text),
            },
            follow_redirects=False,
        )
        task_location = created.headers["location"]
        shown = client.get(task_location)
        response = client.post(
            task_location,
            data={"prompt": too_long, "csrf_token": _extract_csrf_token(shown.text)},
        )

    task_id = task_location.removeprefix("/tasks/")
    task = application.tasks.get(task_id)

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert "prompt must be at most 10000 characters" in response.text
    assert 'maxlength="10000"' in response.text
    assert task.prompt == "how many rs in strawberry"
    assert task.output == "3"


def test_html_sets_csrf_cookie_and_allows_valid_mutations(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        index = client.get("/")
        create_token = _extract_csrf_token(index.text)
        created = client.post(
            "/tasks",
            data={"prompt": "how many rs in strawberry", "csrf_token": create_token},
            follow_redirects=False,
        )
        created_task = client.get(created.headers["location"])
        update_token = _extract_csrf_token(created_task.text)
        updated = client.post(
            created.headers["location"],
            data={"prompt": "how many bs in blueberry", "csrf_token": update_token},
            follow_redirects=False,
        )
        updated_task = client.get(updated.headers["location"])
        delete_token = _extract_csrf_token(updated_task.text)
        deleted = client.post(
            f"{updated.headers['location']}/delete",
            data={"csrf_token": delete_token},
            follow_redirects=False,
        )

    assert "af-csrf=" in index.headers["set-cookie"]
    assert "__Host-" not in index.headers["set-cookie"]
    assert "Domain=" not in index.headers["set-cookie"]
    assert "Path=/" in index.headers["set-cookie"]
    assert "HttpOnly" in index.headers["set-cookie"]
    assert "SameSite=strict" in index.headers["set-cookie"]
    assert "Secure" not in index.headers["set-cookie"]
    assert created.status_code == 303
    assert updated.status_code == 303
    assert deleted.status_code == 303


def test_html_csrf_cookie_sets_secure_only_over_https(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application, scheme="https") as client:
        response = client.get("/")

    assert "af-csrf=" in response.headers["set-cookie"]
    assert "Domain=" not in response.headers["set-cookie"]
    assert "Path=/" in response.headers["set-cookie"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert "Secure" in response.headers["set-cookie"]


def test_html_session_cookie_tracks_flash_state_and_secure_flag(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        index = client.get("/")
        created = client.post(
            "/tasks",
            data={
                "prompt": "how many rs in strawberry",
                "csrf_token": _extract_csrf_token(index.text),
            },
            follow_redirects=False,
        )

    assert "af-session=" in created.headers["set-cookie"]
    assert "HttpOnly" in created.headers["set-cookie"]
    assert "Path=/" in created.headers["set-cookie"]
    assert "SameSite=lax" in created.headers["set-cookie"]
    assert "Secure" not in created.headers["set-cookie"]

    with _http_client(application, scheme="https") as secure_client:
        response = secure_client.get("/")

    assert "af-session=" not in response.headers.get("set-cookie", "")

    with _http_client(application, scheme="https") as secure_client:
        index = secure_client.get("/")
        created = secure_client.post(
            "/tasks",
            data={
                "prompt": "how many rs in strawberry",
                "csrf_token": _extract_csrf_token(index.text),
            },
            follow_redirects=False,
        )

    assert "af-session=" in created.headers["set-cookie"]
    assert "Secure" in created.headers["set-cookie"]


def test_http_sets_security_headers_on_html_rest_and_graphql_responses(
    application,
) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        html = client.get("/")
        rest = client.post("/api/v1/hello")
        graphql = client.post("/graphql", json={"query": "mutation { hello }"})

    for response in (html, rest, graphql):
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "strict-transport-security" not in response.headers

    assert html.headers["content-security-policy"] == (
        "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    )
    assert "content-security-policy" not in rest.headers
    assert "content-security-policy" not in graphql.headers


def test_http_security_headers_wrap_early_rejections_and_https_requests(
    application,
) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        rejected = client.get("/", headers={"host": "evil.invalid"})

    with _http_client(application, scheme="https") as client:
        secure = client.get("/")

    assert rejected.status_code == 400
    assert rejected.headers["x-frame-options"] == "DENY"
    assert rejected.headers["x-content-type-options"] == "nosniff"
    assert rejected.headers["referrer-policy"] == "no-referrer"
    assert "strict-transport-security" not in rejected.headers
    assert secure.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"


def test_html_rejects_csrf_failures_before_mutation(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client_a, _http_client(application) as client_b:
        index_a = client_a.get("/")
        index_b = client_b.get("/")
        token_a = _extract_csrf_token(index_a.text)
        token_b = _extract_csrf_token(index_b.text)

        missing = client_a.post("/tasks", data={"prompt": "how many rs in strawberry"})
        forged = client_a.post(
            "/tasks",
            data={"prompt": "how many rs in strawberry", "csrf_token": "forged"},
        )
        cross_origin = client_a.post(
            "/tasks",
            data={"prompt": "how many rs in strawberry", "csrf_token": token_a},
            headers={"origin": "https://evil.example"},
        )
        created = client_a.post(
            "/tasks",
            data={"prompt": "how many rs in strawberry", "csrf_token": token_a},
            follow_redirects=False,
        )
        task_location = created.headers["location"]
        mismatched_update = client_a.post(
            task_location,
            data={"prompt": "how many bs in blueberry", "csrf_token": token_b},
        )
        shown = client_a.get(task_location)
        delete_token = _extract_csrf_token(shown.text)
        forged_delete = client_a.post(
            f"{task_location}/delete",
            data={"csrf_token": f"{delete_token}forged"},
        )

    task_id = task_location.removeprefix("/tasks/")
    task = application.tasks.get(task_id)

    for response in (missing, forged, cross_origin, mismatched_update, forged_delete):
        assert response.status_code == 403
    assert application.tasks.list() == [task]
    assert task.output == "3"
    assert len(application.traces.list()) == 1


def test_http_accepts_max_unicode_prompt_bodies_but_rejects_10001_in_domain() -> None:
    prompt = "😀" * PROMPT_MAX_LENGTH
    too_long = prompt + "😀"
    application = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    create_calls: list[str] = []

    def create(prompt_value: str) -> Task:
        create_calls.append(prompt_value)
        if len(prompt_value) > PROMPT_MAX_LENGTH:
            raise InvalidPrompt(f"prompt must be at most {PROMPT_MAX_LENGTH} characters")
        return _task(prompt_value, task_id=f"task-{len(create_calls)}")

    application.tasks.create = create  # type: ignore[assignment]

    with _http_client(application) as client:
        index = client.get("/")
        html_ok = client.post(
            "/tasks",
            data={"prompt": prompt, "csrf_token": _extract_csrf_token(index.text)},
            follow_redirects=False,
        )
        html_too_long = client.post(
            "/tasks",
            data={"prompt": too_long, "csrf_token": _extract_csrf_token(index.text)},
        )
        rest_ok = client.post("/api/v1/tasks", json={"prompt": prompt})
        rest_too_long = client.post("/api/v1/tasks", json={"prompt": too_long})
        graphql_ok = client.post(
            "/graphql",
            json={
                "query": "mutation Create($input: TaskInput!) { createTask(input: $input) { id } }",
                "variables": {"input": {"prompt": prompt}},
            },
        )
        graphql_too_long = client.post(
            "/graphql",
            json={
                "query": "mutation Create($input: TaskInput!) { createTask(input: $input) { id } }",
                "variables": {"input": {"prompt": too_long}},
            },
        )

    assert html_ok.status_code == 303
    assert html_too_long.status_code == 422
    assert "at most 10000 characters" in html_too_long.text
    assert rest_ok.status_code == 201
    assert rest_too_long.status_code == 422
    assert rest_too_long.json()["detail"][0]["msg"] == (
        "String should have at most 10000 characters"
    )
    assert graphql_ok.status_code == 200
    assert graphql_ok.json() == {"data": {"createTask": {"id": "task-3"}}}
    assert graphql_too_long.status_code == 200
    assert graphql_too_long.json()["errors"][0]["message"] == (
        "prompt must be at most 10000 characters"
    )
    assert html_ok.status_code != 413
    assert rest_ok.status_code != 413
    assert graphql_ok.status_code != 413
    assert create_calls == [prompt, prompt, prompt, too_long]


def test_rest_crud_status_and_reexecution(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        created_response = client.post(
            "/api/v1/tasks", json={"prompt": "how many rs in strawberry"}
        )
        created = created_response.json()

        listed = client.get("/api/v1/tasks").json()
        fetched = client.get(f"/api/v1/tasks/{created['id']}").json()
        updated_response = client.patch(
            f"/api/v1/tasks/{created['id']}",
            json={"prompt": "how many bs in blueberry"},
        )
        deleted = client.delete(f"/api/v1/tasks/{created['id']}")
        missing = client.get(f"/api/v1/tasks/{created['id']}")

    assert created_response.status_code == 201
    assert created_response.headers["location"] == f"/api/v1/tasks/{created['id']}"
    assert created["output"] == "3"
    assert created["verified"] is True
    assert listed["total"] == 1
    assert fetched == created
    assert updated_response.status_code == 200
    assert updated_response.json()["output"] == "2"
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert missing.status_code == 404


def test_rest_hello_requires_post_and_preserves_safe_method_semantics(
    application,
) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        get_response = client.get("/api/v1/hello")
        post_response = client.post("/api/v1/hello")

    assert get_response.status_code == 405
    assert post_response.status_code == 200
    assert post_response.json() == {"output": "Hello, world!", "verified": True}
    assert application.tasks.list() == []
    assert len(application.traces.list()) == 1


def test_rest_rejects_blank_prompt(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        response = client.post("/api/v1/tasks", json={"prompt": "   "})

    assert response.status_code == 422
    assert application.tasks.list() == []


def test_rest_rejects_too_long_prompt(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        response = client.post(
            "/api/v1/tasks",
            json={"prompt": "a" * (PROMPT_MAX_LENGTH + 1)},
        )

    assert response.status_code == 422
    assert application.tasks.list() == []


def test_graphql_crud_shares_rest_repository(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        hello_response = client.post("/graphql", json={"query": "mutation { hello }"})
        create_response = client.post(
            "/graphql",
            json={
                "query": """
                    mutation Create($input: TaskInput!) {
                      createTask(input: $input) { id prompt output intent route verified }
                    }
                """,
                "variables": {"input": {"prompt": "how many rs in strawberry"}},
            },
        )
        created_payload = create_response.json()
        created = created_payload["data"]["createTask"]

        rest_value = client.get(f"/api/v1/tasks/{created['id']}").json()
        list_response = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 10, offset: 0) { id output verified } }"},
        ).json()
        update_response = client.post(
            "/graphql",
            json={
                "query": """
                    mutation Update($id: ID!, $input: TaskInput!) {
                      updateTask(id: $id, input: $input) { id output }
                    }
                """,
                "variables": {
                    "id": created["id"],
                    "input": {"prompt": "how many ps in pineapple"},
                },
            },
        ).json()
        delete_response = client.post(
            "/graphql",
            json={
                "query": "mutation Delete($id: ID!) { deleteTask(id: $id) }",
                "variables": {"id": created["id"]},
            },
        ).json()

    assert hello_response.status_code == 200
    assert hello_response.json() == {"data": {"hello": "Hello, world!"}}
    assert "errors" not in created_payload
    assert created["output"] == "3"
    assert rest_value["id"] == created["id"]
    assert list_response["data"]["tasks"] == [
        {"id": created["id"], "output": "3", "verified": True}
    ]
    assert update_response["data"]["updateTask"]["output"] == "3"
    assert delete_response["data"]["deleteTask"] is True


def test_graphql_missing_task_and_mutation_errors(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        missing_query = client.post(
            "/graphql",
            json={
                "query": "query Task($id: ID!) { task(id: $id) { id } }",
                "variables": {"id": "missing"},
            },
        )
        update_missing = client.post(
            "/graphql",
            json={
                "query": """
                    mutation Update($id: ID!, $input: TaskInput!) {
                      updateTask(id: $id, input: $input) { id }
                    }
                """,
                "variables": {
                    "id": "missing",
                    "input": {"prompt": "hello"},
                },
            },
        )
        delete_missing = client.post(
            "/graphql",
            json={
                "query": "mutation Delete($id: ID!) { deleteTask(id: $id) }",
                "variables": {"id": "missing"},
            },
        )

    assert missing_query.status_code == 200
    assert missing_query.json() == {"data": {"task": None}}
    for response, field in (
        (update_missing, "updateTask"),
        (delete_missing, "deleteTask"),
    ):
        assert response.status_code == 200
        payload = response.json()
        assert payload["data"] is None
        assert payload["errors"][0]["message"] == "task not found: missing"
        assert payload["errors"][0]["path"] == [field]
        assert payload["errors"][0]["extensions"] == {"code": "TASKNOTFOUND"}


def test_graphql_rejects_blank_task_prompt(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        response = client.post(
            "/graphql",
            json={
                "query": """
                    mutation Create($input: TaskInput!) {
                      createTask(input: $input) { id }
                    }
                """,
                "variables": {"input": {"prompt": "   "}},
            },
        )
        listed = client.post("/graphql", json={"query": "{ tasks { id } }"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] is None
    assert payload["errors"][0]["message"] == "prompt must not be blank"
    assert payload["errors"][0]["path"] == ["createTask"]
    assert payload["errors"][0]["extensions"] == {"code": "INVALIDPROMPT"}
    assert listed.json() == {"data": {"tasks": []}}


def test_graphql_rejects_too_long_task_prompt(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        response = client.post(
            "/graphql",
            json={
                "query": """
                    mutation Create($input: TaskInput!) {
                      createTask(input: $input) { id }
                    }
                """,
                "variables": {"input": {"prompt": "a" * (PROMPT_MAX_LENGTH + 1)}},
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] is None
    assert payload["errors"][0]["message"] == (
        f"prompt must be at most {PROMPT_MAX_LENGTH} characters"
    )
    assert payload["errors"][0]["path"] == ["createTask"]
    assert payload["errors"][0]["extensions"] == {"code": "INVALIDPROMPT"}


def test_graphql_sanitizes_unexpected_resolver_failures() -> None:
    class ExplodingController:
        def list(self, *, limit=None, offset=0):  # type: ignore[no-untyped-def]
            raise RuntimeError("SECRET-database-url")

        def create(self, prompt: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("SECRET-database-url")

        def execute(self, prompt: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("SECRET-database-url")

    with _graphql_client(ExplodingController()) as client:
        list_response = client.post("/graphql", json={"query": "{ tasks { id } }"})
        create_response = client.post(
            "/graphql",
            json={
                "query": "mutation Create($input: TaskInput!) { createTask(input: $input) { id } }",
                "variables": {"input": {"prompt": "how many rs in strawberry"}},
            },
        )
        hello_response = client.post("/graphql", json={"query": "mutation { hello }"})

    for response, path in (
        (list_response, ["tasks"]),
        (create_response, ["createTask"]),
        (hello_response, ["hello"]),
    ):
        payload = response.json()
        assert response.status_code == 200
        assert "SECRET" not in response.text
        assert payload["data"] is None
        assert payload["errors"][0]["message"] == "internal GraphQL adapter error"
        assert payload["errors"][0]["extensions"] == {"code": "INTERNAL_ERROR"}
        assert payload["errors"][0]["path"] == path


def test_graphql_rejects_oversized_body_before_execution() -> None:
    class RecordingController:
        def __init__(self) -> None:
            self.calls = 0

        def create(self, prompt: str):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise AssertionError(prompt)

    controller = RecordingController()
    oversized_prompt = "x" * HTTP_MAX_BODY_BYTES

    with _graphql_client(controller) as client:
        response = client.post(
            "/graphql",
            json={
                "query": "mutation Create($input: TaskInput!) { createTask(input: $input) { id } }",
                "variables": {"input": {"prompt": oversized_prompt}},
            },
        )

    assert response.status_code == 413
    assert response.json() == {
        "data": None,
        "errors": [
            {
                "message": f"graphql request body must be at most {HTTP_MAX_BODY_BYTES} bytes",
                "extensions": {"code": "REQUEST_TOO_LARGE"},
            }
        ],
    }
    assert controller.calls == 0


def test_http_rejects_oversized_html_and_rest_bodies_before_mutation(application) -> None:  # type: ignore[no-untyped-def]
    oversized_form_prompt = "a" * HTTP_MAX_BODY_BYTES
    oversized_json_prompt = "a" * HTTP_MAX_BODY_BYTES

    with _http_client(application) as client:
        index = client.get("/")
        html = client.post(
            "/tasks",
            data={"prompt": oversized_form_prompt, "csrf_token": _extract_csrf_token(index.text)},
        )
        rest = client.post("/api/v1/tasks", json={"prompt": oversized_json_prompt})

    assert html.status_code == 413
    assert rest.status_code == 413
    assert application.tasks.list() == []
    assert application.traces.list() == []


def test_graphql_rejects_alias_bombs_before_execution() -> None:
    class RecordingController:
        def __init__(self) -> None:
            self.calls = 0

        def list(self, *, limit=None, offset=0):  # type: ignore[no-untyped-def]
            self.calls += 1
            return []

    controller = RecordingController()
    aliases = " ".join(f"a{i}: tasks(limit: 1, offset: 0) {{ id }}" for i in range(51))

    with _graphql_client(controller) as client:
        response = client.post("/graphql", json={"query": f"{{ {aliases} }}"})

    assert response.status_code == 200
    assert response.json() == {
        "data": None,
        "errors": [
            {
                "message": "graphql query exceeds the configured complexity limits",
                "extensions": {"code": "QUERY_TOO_COMPLEX"},
            }
        ],
    }
    assert controller.calls == 0


def test_graphql_rejects_fragment_dag_and_cycle_before_execution() -> None:
    class RecordingController:
        def __init__(self) -> None:
            self.calls = 0

        def list(self, *, limit=None, offset=0):  # type: ignore[no-untyped-def]
            self.calls += 1
            return []

    controller = RecordingController()
    dag_fragments = "\n".join(
        [
            "fragment F0 on Query { a0: tasks(limit: 1, offset: 0) { id } }",
            *[f"fragment F{i} on Query {{ ...F{i - 1} ...F{i - 1} }}" for i in range(1, 17)],
        ]
    )
    dag_query = f"query Example {{ ...F16 }}\n{dag_fragments}"
    cycle_query = """
        query Example { ...LoopA }
        fragment LoopA on Query { ...LoopB }
        fragment LoopB on Query { ...LoopA }
    """

    with _graphql_client(controller) as client:
        dag = client.post("/graphql", json={"query": dag_query})
        cycle = client.post("/graphql", json={"query": cycle_query})

    for response in (dag, cycle):
        assert response.status_code == 200
        assert response.json() == {
            "data": None,
            "errors": [
                {
                    "message": "graphql query exceeds the configured complexity limits",
                    "extensions": {"code": "QUERY_TOO_COMPLEX"},
                }
            ],
        }
    assert controller.calls == 0


def test_graphql_rejects_excessive_task_node_budget_before_execution() -> None:
    class RecordingController:
        def __init__(self) -> None:
            self.calls = 0

        def list(self, *, limit=None, offset=0):  # type: ignore[no-untyped-def]
            self.calls += 1
            return []

    controller = RecordingController()
    aliases = " ".join(f"a{i}: tasks(limit: 100, offset: 0) {{ prompt output }}" for i in range(3))

    with _graphql_client(controller) as client:
        response = client.post("/graphql", json={"query": f"{{ {aliases} }}"})

    assert response.status_code == 200
    assert response.json() == {
        "data": None,
        "errors": [
            {
                "message": "graphql query exceeds the configured complexity limits",
                "extensions": {"code": "QUERY_TOO_COMPLEX"},
            }
        ],
    }
    assert controller.calls == 0


def test_graphql_get_rejects_non_query_operations_before_execution(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        mutation = client.get(
            "/graphql",
            params={
                "query": """
                    mutation CreateTask {
                      createTask(input: {prompt: "how many rs in strawberry"}) { id }
                    }
                """,
                "operationName": "CreateTask",
            },
        )
        subscription = client.get(
            "/graphql",
            params={"query": "subscription WatchTasks { tasks { id } }"},
        )
        multi_operation_query = client.get(
            "/graphql",
            params={
                "query": """
                    query ReadTasks { ...TaskFields }
                    mutation CreateTask {
                      createTask(input: {prompt: "how many rs in strawberry"}) { id }
                    }
                    fragment TaskFields on Query {
                      tasks(limit: 10, offset: 0) { id }
                    }
                """,
                "operationName": "ReadTasks",
            },
        )

    for response in (mutation, subscription):
        assert response.status_code == 405
        assert response.json() == {
            "data": None,
            "errors": [
                {
                    "message": "GET requests may only execute query operations",
                    "extensions": {"code": "METHOD_NOT_ALLOWED"},
                }
            ],
        }
    assert multi_operation_query.status_code == 200
    assert multi_operation_query.json() == {"data": {"tasks": []}}
    assert application.tasks.list() == []
    assert application.traces.list() == []


def test_graphql_get_queries_leave_task_and_trace_stores_unchanged(
    application,
) -> None:  # type: ignore[no-untyped-def]
    created = application.tasks.create("how many rs in strawberry")
    before_tasks = application.tasks.list()
    before_trace_count = len(application.traces.list())

    with _http_client(application) as client:
        listed = client.get(
            "/graphql",
            params={"query": "{ tasks(limit: 10, offset: 0) { id output } }"},
        )
        fetched = client.get(
            "/graphql",
            params={
                "query": "query Task($id: ID!) { task(id: $id) { id output } }",
                "variables": f'{{"id":"{created.id}"}}',
                "operationName": "Task",
            },
        )
        hello = client.get("/graphql", params={"query": "{ hello }"})

    assert listed.status_code == 200
    assert listed.json() == {"data": {"tasks": [{"id": created.id, "output": "3"}]}}
    assert fetched.status_code == 200
    assert fetched.json() == {"data": {"task": {"id": created.id, "output": "3"}}}
    assert hello.status_code == 200
    assert hello.json()["errors"][0]["message"] == "Cannot query field 'hello' on type 'Query'."
    assert application.tasks.list() == before_tasks
    assert len(application.traces.list()) == before_trace_count


def test_http_rejects_untrusted_host_headers_before_html_and_graphql_execution(
    application,
) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        html = client.get("/", headers={"host": "evil.invalid"})
        graphql = client.get(
            "/graphql",
            params={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"host": "evil.invalid"},
        )

    assert html.status_code == 400
    assert graphql.status_code == 400
    assert application.tasks.list() == []
    assert application.traces.list() == []


def test_http_requires_explicit_allowed_hosts_for_non_loopback_bind() -> None:
    application = create_application(
        settings=Settings(host="0.0.0.0"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )

    with pytest.raises(Exception, match="allowed host"):
        create_http_app(application)


def test_http_allows_configured_host_for_non_loopback_bind() -> None:
    application = create_application(
        settings=Settings(host="0.0.0.0", allowed_hosts=("af.internal",)),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    client = TestClient(create_http_app(application), base_url="http://af.internal")
    try:
        allowed = client.get("/", headers={"host": "af.internal"})
        blocked = client.get("/", headers={"host": "evil.invalid"})
    finally:
        client.close()

    assert allowed.status_code == 200
    assert blocked.status_code == 400


def test_http_accepts_loopback_ipv6_and_configured_hosts_with_ports() -> None:
    loopback = create_application(
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    with _http_client(loopback) as client:
        for host in ("[::1]:8000", "[::1]", "::1", "127.0.0.1:8000", "localhost:8000"):
            response = client.get("/", headers={"host": host})
            assert response.status_code == 200

        assert client.get("/", headers={"host": "evil.invalid"}).status_code == 400

    lan = create_application(
        settings=Settings(host="0.0.0.0", allowed_hosts=("af.internal", "192.168.1.20")),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    with _http_client(lan, base_url="http://af.internal:8000") as client:
        for host in (
            "af.internal",
            "af.internal:8000",
            "192.168.1.20",
            "192.168.1.20:8000",
        ):
            response = client.get("/", headers={"host": host})
            assert response.status_code == 200

        assert client.get("/", headers={"host": "evil.invalid:8000"}).status_code == 400


def test_graphql_tasks_paginates_at_repository_boundary() -> None:
    class RecordingTaskRepository(InMemoryTaskRepository):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[tuple[int | None, int]] = []

        def list(self, *, limit: int | None = None, offset: int = 0):  # type: ignore[override]
            self.calls.append((limit, offset))
            return super().list(limit=limit, offset=offset)

    repository = RecordingTaskRepository()
    application = create_application(
        task_repository=repository,
        trace_repository=InMemoryTraceRepository(),
    )
    application.tasks.create("how many rs in strawberry")
    application.tasks.create("how many bs in blueberry")

    with _http_client(application) as client:
        response = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 1) { prompt output } }"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "data": {"tasks": [{"prompt": "how many bs in blueberry", "output": "2"}]}
    }
    assert repository.calls[-1] == (1, 1)


def test_graphql_rejects_invalid_pagination_bounds(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        response = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 101, offset: -1) { id } }"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"] is None
    assert payload["errors"][0]["message"] == "limit must be between 1 and 100"
    assert payload["errors"][0]["path"] == ["tasks"]
    assert payload["errors"][0]["extensions"] == {"code": "INVALIDPAGINATION"}


def test_graphql_negotiates_media_types_and_rejects_unsupported_accept(application) -> None:  # type: ignore[no-untyped-def]
    with _http_client(application) as client:
        negotiated = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/graphql-response+json"},
        )
        legacy = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/json"},
        )
        graphql_q_zero = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/graphql-response+json;q=0"},
        )
        json_q_zero = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "text/plain, application/json;q=0"},
        )
        prefer_json = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/json, application/graphql-response+json;q=0.5"},
        )
        wildcard = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/*;q=0.8"},
        )
        wildcard_with_exact_graphql_rejection = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/graphql-response+json;q=0, application/*;q=1"},
        )
        wildcard_with_exact_json_rejection = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/json;q=0, application/*;q=1"},
        )
        global_wildcard_with_exact_graphql_rejection = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/graphql-response+json;q=0, */*;q=1"},
        )
        global_wildcard_with_exact_json_rejection = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/json;q=0, */*;q=1"},
        )
        global_wildcard_with_application_rejection = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/*;q=0, */*;q=1"},
        )
        unsupported = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "text/plain"},
        )
        malformed_accept = client.post(
            "/graphql",
            json={"query": "{ tasks(limit: 1, offset: 0) { id } }"},
            headers={"accept": "application/graphql-response+json;q=0;q=1"},
        )
        wrong_type = client.post(
            "/graphql",
            content='{"query":"{ tasks(limit: 1, offset: 0) { id } }"}',
            headers={"content-type": "application/jsonp"},
        )
        malformed_content_type = client.post(
            "/graphql",
            content='{"query":"{ tasks(limit: 1, offset: 0) { id } }"}',
            headers={"content-type": "application/json; garbage"},
        )
        unterminated_quoted_content_type = client.post(
            "/graphql",
            content='{"query":"{ tasks(limit: 1, offset: 0) { id } }"}',
            headers={"content-type": 'application/json; charset="unterminated'},
        )
        invalid_token_content_type = client.post(
            "/graphql",
            content='{"query":"{ tasks(limit: 1, offset: 0) { id } }"}',
            headers={"content-type": "application/json; charset=utf 8"},
        )
        duplicate_charset_content_type = client.post(
            "/graphql",
            content='{"query":"{ tasks(limit: 1, offset: 0) { id } }"}',
            headers={"content-type": "application/json; charset=utf-8; charset=utf-16"},
        )
        get_mutation = client.get(
            "/graphql",
            params={
                "query": (
                    "mutation CreateTask { "
                    'createTask(input: {prompt: "how many rs in strawberry"}) { id }'
                    " }"
                ),
                "operationName": "CreateTask",
            },
            headers={"accept": "application/graphql-response+json"},
        )

    assert negotiated.status_code == 200
    assert negotiated.headers["content-type"].startswith("application/graphql-response+json")
    assert legacy.status_code == 200
    assert legacy.headers["content-type"].startswith("application/json")
    assert graphql_q_zero.status_code == 406
    assert json_q_zero.status_code == 406
    assert prefer_json.status_code == 200
    assert prefer_json.headers["content-type"].startswith("application/json")
    assert wildcard.status_code == 200
    assert wildcard.headers["content-type"].startswith("application/graphql-response+json")
    assert wildcard_with_exact_graphql_rejection.status_code == 200
    assert wildcard_with_exact_graphql_rejection.headers["content-type"].startswith(
        "application/json"
    )
    assert wildcard_with_exact_json_rejection.status_code == 200
    assert wildcard_with_exact_json_rejection.headers["content-type"].startswith(
        "application/graphql-response+json"
    )
    assert global_wildcard_with_exact_graphql_rejection.status_code == 200
    assert global_wildcard_with_exact_graphql_rejection.headers["content-type"].startswith(
        "application/json"
    )
    assert global_wildcard_with_exact_json_rejection.status_code == 200
    assert global_wildcard_with_exact_json_rejection.headers["content-type"].startswith(
        "application/graphql-response+json"
    )
    assert global_wildcard_with_application_rejection.status_code == 406
    assert unsupported.status_code == 406
    assert unsupported.json()["errors"][0]["extensions"] == {"code": "NOT_ACCEPTABLE"}
    assert malformed_accept.status_code == 406
    assert wrong_type.status_code == 415
    assert wrong_type.json()["errors"][0]["extensions"] == {"code": "UNSUPPORTED_MEDIA_TYPE"}
    assert malformed_content_type.status_code == 415
    assert malformed_content_type.json()["errors"][0]["extensions"] == {
        "code": "UNSUPPORTED_MEDIA_TYPE"
    }
    assert unterminated_quoted_content_type.status_code == 415
    assert invalid_token_content_type.status_code == 415
    assert duplicate_charset_content_type.status_code == 415
    assert get_mutation.status_code == 405
    assert get_mutation.headers["allow"] == "POST"


def test_graphql_sanitizes_task_presentation_failures() -> None:
    class CorruptTask:
        id = "task-1"
        prompt = "how many rs in strawberry"

        @property
        def output(self) -> str:
            raise RuntimeError("SECRET-task-output")

    class CorruptController:
        def list(self, *, limit=None, offset=0):  # type: ignore[no-untyped-def]
            return [CorruptTask()]

    with _graphql_client(CorruptController()) as client:
        response = client.post("/graphql", json={"query": "{ tasks(limit: 1, offset: 0) { id } }"})

    assert response.status_code == 200
    assert "SECRET" not in response.text
    assert response.json()["errors"][0]["message"] == "internal GraphQL adapter error"


def test_http_host_owns_only_internally_created_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    monkeypatch.setattr(http_module, "create_application", lambda: owned)
    with TestClient(create_http_app(), base_url=f"http://{TEST_HOST}"):
        pass
    assert owned.lifecycle.state is ApplicationState.SHUTDOWN

    injected = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    with TestClient(create_http_app(injected), base_url=f"http://{TEST_HOST}"):
        pass
    assert injected.lifecycle.state is ApplicationState.SERVING
    injected.shutdown()


def test_http_construction_failure_closes_internally_created_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    monkeypatch.setattr(http_module, "create_application", lambda: owned)

    with pytest.raises(InvalidProviderConfiguration):
        create_http_app(bind_host="0.0.0.0", allowed_hosts=())

    assert owned.lifecycle.state is ApplicationState.SHUTDOWN


def test_http_invalid_web_profile_closes_internally_created_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
    )
    monkeypatch.setattr(http_module, "create_application", lambda: owned)

    with pytest.raises(
        InvalidProviderConfiguration,
        match=(r"web profile must be one of basic, experimental, pretty; got 'invalid'"),
    ):
        create_http_app(web_profile="invalid")

    assert owned.lifecycle.state is ApplicationState.SHUTDOWN


def test_http_construction_failure_bridges_owned_async_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Provider(ServiceProvider):
        provider_id = "test.http-async-shutdown"

        def register(self, registrar: ProviderRegistrar) -> None:
            del registrar

        async def shutdown(self, container: Container) -> None:
            del container
            events.append("shutdown")

    owned = create_application(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        task_repository=InMemoryTaskRepository(),
        trace_repository=InMemoryTraceRepository(),
        providers=(Provider(),),
    )
    monkeypatch.setattr(http_module, "create_application", lambda: owned)

    with pytest.raises(InvalidProviderConfiguration):
        create_http_app(bind_host="0.0.0.0", allowed_hosts=())

    assert owned.lifecycle.state is ApplicationState.SHUTDOWN
    assert events == ["shutdown"]
