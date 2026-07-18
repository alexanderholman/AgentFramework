from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.testclient import TestClient

from af.bootstrap import AFApplication, create_application
from af.framework.types import PROMPT_MAX_LENGTH
from af.infrastructure.repositories import InMemoryTaskRepository
from af.infrastructure.tracing import InMemoryTraceRepository
from af.interfaces.graphql import create_graphql_router
from af.interfaces.http import create_http_app

CSRF_PATTERN = re.compile(r'name="csrf_token" value="([^"]+)"')
TEST_HOST = "127.0.0.1"


def _extract_csrf_token(html: str) -> str:
    match = CSRF_PATTERN.search(html)
    assert match is not None
    return match.group(1)


def _graphql_client(controller: object) -> TestClient:
    app = FastAPI()
    app.include_router(create_graphql_router(controller), prefix="/graphql")
    return TestClient(app, base_url=f"http://{TEST_HOST}")


def _http_client(application: AFApplication, *, scheme: str = "http") -> TestClient:
    return TestClient(create_http_app(application), base_url=f"{scheme}://{TEST_HOST}")


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
        update_token = _extract_csrf_token(shown.text)
        update_response = client.post(
            task_location,
            data={"prompt": "how many bs in blueberry", "csrf_token": update_token},
            follow_redirects=False,
        )
        updated = client.get(task_location)
        delete_token = _extract_csrf_token(updated.text)
        delete_response = client.post(
            f"{task_location}/delete",
            data={"csrf_token": delete_token},
            follow_redirects=False,
        )
        deleted_list = client.get("/tasks")
        missing = client.get(task_location)

    assert empty_list.status_code == 200
    assert "No persisted tasks yet." in empty_list.text
    assert create_response.status_code == 303
    assert task_location.startswith("/tasks/")
    assert "how many rs in strawberry" in populated_list.text
    assert "→ 3" in populated_list.text
    assert shown.status_code == 200
    assert 'data-testid="answer">3<' in shown.text
    assert 'maxlength="10000"' in shown.text
    assert update_response.status_code == 303
    assert update_response.headers["location"] == task_location
    assert "how many bs in blueberry" in updated.text
    assert 'data-testid="answer">2<' in updated.text
    assert delete_response.status_code == 303
    assert delete_response.headers["location"] == "/tasks"
    assert "No persisted tasks yet." in deleted_list.text
    assert missing.status_code == 404
    assert missing.json() == {"detail": f"task not found: {task_location.removeprefix('/tasks/')}"}


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
    oversized_prompt = "x" * 40_000

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
                "message": "graphql request body must be at most 32768 bytes",
                "extensions": {"code": "REQUEST_TOO_LARGE"},
            }
        ],
    }
    assert controller.calls == 0


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
