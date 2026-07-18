from __future__ import annotations

from fastapi.testclient import TestClient

from af.interfaces.http import create_http_app


def test_html_index_and_form_answer(application) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_http_app(application)) as client:
        index = client.get("/")
        created = client.post(
            "/tasks",
            data={"prompt": "how many rs in strawberry"},
            follow_redirects=True,
        )

    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "AF Agent Framework" in index.text
    assert created.status_code == 200
    assert 'data-testid="answer">3<' in created.text


def test_html_task_lifecycle(application) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_http_app(application)) as client:
        empty_list = client.get("/tasks")
        create_response = client.post(
            "/tasks",
            data={"prompt": "how many rs in strawberry"},
            follow_redirects=False,
        )
        task_location = create_response.headers["location"]

        populated_list = client.get("/tasks")
        shown = client.get(task_location)
        update_response = client.post(
            task_location,
            data={"prompt": "how many bs in blueberry"},
            follow_redirects=False,
        )
        updated = client.get(task_location)
        delete_response = client.post(
            f"{task_location}/delete",
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
    assert update_response.status_code == 303
    assert update_response.headers["location"] == task_location
    assert "how many bs in blueberry" in updated.text
    assert 'data-testid="answer">2<' in updated.text
    assert delete_response.status_code == 303
    assert delete_response.headers["location"] == "/tasks"
    assert "No persisted tasks yet." in deleted_list.text
    assert missing.status_code == 404
    assert missing.json() == {"detail": f"task not found: {task_location.removeprefix('/tasks/')}"}


def test_rest_crud_status_and_reexecution(application) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_http_app(application)) as client:
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


def test_rest_rejects_blank_prompt(application) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_http_app(application)) as client:
        response = client.post("/api/v1/tasks", json={"prompt": "   "})

    assert response.status_code == 422
    assert application.tasks.list() == []


def test_graphql_crud_shares_rest_repository(application) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_http_app(application)) as client:
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
            json={"query": "{ tasks { id output verified } }"},
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
    with TestClient(create_http_app(application)) as client:
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
    with TestClient(create_http_app(application)) as client:
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
