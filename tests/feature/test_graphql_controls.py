from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event

from af.application.task_guard import TaskOperationCancelled
from af.application.tasks import TaskController, TaskService
from af.framework.context import ExecutionContext, use_context
from af.framework.types import WorkRequest
from af.infrastructure.database import Database
from af.infrastructure.repositories import InMemoryTaskRepository, SqlAlchemyTaskRepository
from af.infrastructure.sqlite_queries import SQLitePersistedQueryRegistry
from af.interfaces.graphql import approve_graphql_query, create_graphql_router


def client(controller, **options):  # type: ignore[no-untyped-def]
    app = FastAPI()
    app.include_router(create_graphql_router(controller, **options), prefix="/graphql")
    return TestClient(app)


def persisted(digest: str) -> dict[str, object]:
    return {"extensions": {"persistedQuery": {"version": 1, "sha256Hash": digest}}}


def test_approved_query_restart_revoke_and_no_automatic_registration(application, tmp_path):  # type: ignore[no-untyped-def]
    path = tmp_path / "queries.sqlite3"
    query = "query Tasks { tasks { id } }"
    with SQLitePersistedQueryRegistry(path) as registry:
        approved = approve_graphql_query(registry, query)
    with (
        SQLitePersistedQueryRegistry(path) as reopened,
        client(
            application.tasks, persisted_queries=reopened, require_persisted_queries=True
        ) as http,
    ):
        body = persisted(approved.digest)
        assert http.post("/graphql", json=body).json() == {"data": {"tasks": []}}
        assert http.get(
            "/graphql", params={"extensions": json.dumps(body["extensions"])}
        ).json() == {"data": {"tasks": []}}
        assert (
            http.post("/graphql", json={"query": query}).json()["errors"][0]["extensions"]["code"]
            == "PERSISTED_QUERY_REQUIRED"
        )
        assert (
            http.post("/graphql", json={**body, "query": "{ tasks { prompt } }"}).status_code == 400
        )
        reopened.revoke(approved.digest)
        assert (
            http.post("/graphql", json={**body, "query": query}).json()["errors"][0]["extensions"][
                "code"
            ]
            == "PERSISTED_QUERY_NOT_FOUND"
        )
    with SQLitePersistedQueryRegistry(path) as reopened:
        assert reopened.resolve(approved.digest) is None


def test_authorizes_whole_operation_before_first_mutation(application):  # type: ignore[no-untyped-def]
    seen = []

    def authorize(context, operation, fields):  # type: ignore[no-untyped-def]
        seen.append((operation, fields))
        return "deleteTask" not in fields

    query = (
        'mutation { first: createTask(input: {prompt:"hello"}) { id } ...Later } '
        'fragment Later on Mutation { deleteTask(id:"absent") }'
    )
    with client(application.tasks, authorize_operation=authorize) as http:
        response = http.post("/graphql", json={"query": query})
    assert response.status_code == 403
    assert response.json()["errors"][0]["extensions"]["code"] == "FORBIDDEN"
    assert seen == [("mutation", frozenset({"createTask", "deleteTask"}))]
    assert application.tasks.list() == []


def test_task_loader_batches_sql_and_does_not_cache_between_requests(application):  # type: ignore[no-untyped-def]
    first = application.tasks.create("hello")
    second = application.tasks.create("how many rs in strawberry")
    database = Database("sqlite://")
    repository = SqlAlchemyTaskRepository(database)
    repository.save(first)
    repository.save(second)
    statements = []
    event.listen(
        database.engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: statements.append(statement),
    )
    controller = TaskController(TaskService(application.agent, repository))
    query = (
        f'{{ a:task(id:"{first.id}"){{id}} b:task(id:"{second.id}"){{id}} '
        f'c:task(id:"{first.id}"){{id}} missing:task(id:"absent"){{id}} }}'
    )
    try:
        with client(controller) as http:
            response = http.post("/graphql", json={"query": query}).json()
            assert response == {
                "data": {
                    "a": {"id": first.id},
                    "b": {"id": second.id},
                    "c": {"id": first.id},
                    "missing": None,
                }
            }
            assert len([sql for sql in statements if sql.startswith("SELECT")]) == 1
            repository.delete(first.id)
            assert http.post("/graphql", json={"query": query}).json()["data"]["a"] is None
    finally:
        database.close()


def test_timeout_returns_before_agent_and_fences_late_task_write(application):  # type: ignore[no-untyped-def]
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    outcome = application.agent.handle(WorkRequest(prompt="hello"))

    class SlowAgent:
        def handle(self, request):  # type: ignore[no-untyped-def]
            entered.set()
            release.wait(2)
            return outcome

    class ObservedController(TaskController):
        def create(self, prompt):  # type: ignore[no-untyped-def]
            try:
                return super().create(prompt)
            finally:
                finished.set()

    repository = InMemoryTaskRepository()
    controller = ObservedController(TaskService(SlowAgent(), repository))
    with client(controller, operation_timeout_seconds=0.03) as http:
        try:
            response = http.post(
                "/graphql", json={"query": 'mutation { createTask(input:{prompt:"hello"}) { id } }'}
            )
            assert entered.is_set() and not finished.is_set()
            assert response.json()["errors"][0]["extensions"]["code"] == "OPERATION_TIMEOUT"
        finally:
            release.set()
    assert finished.wait(1)
    assert repository.list() == []


def test_installed_http_exposes_persisted_query_policy(application, tmp_path):  # type: ignore[no-untyped-def]
    from af.interfaces.http import create_http_app

    with SQLitePersistedQueryRegistry(tmp_path / "installed.sqlite") as registry:
        approved = approve_graphql_query(registry, "{ tasks { id } }")
        app = create_http_app(
            application, graphql_persisted_queries=registry, graphql_require_persisted_queries=True
        )
        with TestClient(app, base_url="http://127.0.0.1") as http:
            assert http.post("/graphql", json=persisted(approved.digest)).json() == {
                "data": {"tasks": []}
            }
            assert (
                http.post("/graphql", json={"query": "{ tasks { id } }"}).json()["errors"][0][
                    "extensions"
                ]["code"]
                == "PERSISTED_QUERY_REQUIRED"
            )


def test_persisted_scope_schema_and_operation_are_bound(application, tmp_path):  # type: ignore[no-untyped-def]
    from af.framework.persisted_queries import PersistedQuery

    with SQLitePersistedQueryRegistry(tmp_path / "scope.sqlite") as registry:
        approved = approve_graphql_query(
            registry, "query A { tasks { id } }", operation_name="A", tenant_id="tenant-a"
        )
        with client(application.tasks, persisted_queries=registry) as http:
            body = {**persisted(approved.digest), "operationName": "A"}
            assert (
                http.post("/graphql", json=body).json()["errors"][0]["extensions"]["code"]
                == "PERSISTED_QUERY_NOT_FOUND"
            )
        scoped = registry.resolve(approved.digest, operation_name="A", tenant_id="tenant-a")
        assert scoped == approved
        assert (
            registry.resolve(
                approved.digest, operation_name="A", tenant_id="tenant-a", policy_version="2"
            )
            is None
        )
        stale = PersistedQuery("{ tasks { id } }", "0" * 64)
        registry.approve(stale)
        with client(application.tasks, persisted_queries=registry) as http:
            assert (
                http.post("/graphql", json=persisted(stale.digest)).json()["errors"][0][
                    "extensions"
                ]["code"]
                == "PERSISTED_QUERY_STALE"
            )


@pytest.mark.parametrize(
    "extension",
    [
        [],
        {"persistedQuery": []},
        {"persistedQuery": {"version": True, "sha256Hash": "0" * 64}},
        {"persistedQuery": {"version": 1, "sha256Hash": "bad"}},
        {"persistedQuery": {"version": 1, "sha256Hash": 42}},
    ],
)
def test_malformed_persisted_extensions_are_rejected(application, extension):  # type: ignore[no-untyped-def]
    with client(application.tasks) as http:
        response = http.post(
            "/graphql", json={"query": "{ tasks { id } }", "extensions": extension}
        )
        assert response.status_code == 400


def test_persisted_get_stays_read_only_and_duplicate_parameters_fail(application, tmp_path):  # type: ignore[no-untyped-def]
    with SQLitePersistedQueryRegistry(tmp_path / "get.sqlite") as registry:
        approved = approve_graphql_query(
            registry, 'mutation { createTask(input:{prompt:"hello"}) { id } }'
        )
        with client(application.tasks, persisted_queries=registry) as http:
            assert (
                http.get(
                    "/graphql",
                    params={"extensions": json.dumps(persisted(approved.digest)["extensions"])},
                ).status_code
                == 405
            )
            assert http.get("/graphql", params={"extensions": "{"}).status_code == 400
            assert http.get("/graphql?query=a&query=b").status_code == 400
        assert application.tasks.list() == []


def test_authorizer_exception_is_denial_and_validation_happens_first(application):  # type: ignore[no-untyped-def]
    calls = []

    def broken(context, operation, fields):  # type: ignore[no-untyped-def]
        calls.append(fields)
        raise RuntimeError("PRIVATE-authorizer-secret")

    with client(application.tasks, authorize_operation=broken) as http:
        invalid = http.post("/graphql", json={"query": "{ unknownField }"})
        assert invalid.json()["errors"] and not calls
        denied = http.post("/graphql", json={"query": "{ ... on Query { tasks { id } } }"})
        assert denied.status_code == 403
        assert "PRIVATE" not in denied.text
        assert calls == [frozenset({"tasks"})]


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan"), 601])
def test_router_rejects_unbounded_timeouts(application, timeout):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        create_graphql_router(application.tasks, operation_timeout_seconds=timeout)


def test_router_configuration_and_registry_fail_closed(application, tmp_path):  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError):
        create_graphql_router(application.tasks, require_persisted_queries=True)
    with pytest.raises(ValueError):
        create_graphql_router(application.tasks, query_policy_version="")
    registry = SQLitePersistedQueryRegistry(tmp_path / "closed.sqlite")
    registry.close()
    with client(application.tasks, persisted_queries=registry) as http:
        response = http.post("/graphql", json=persisted("0" * 64))
        assert response.status_code == 503
        assert response.json()["errors"][0]["extensions"]["code"] == "PERSISTED_QUERY_UNAVAILABLE"


def test_already_expired_context_runs_no_resolver(application):  # type: ignore[no-untyped-def]
    app = FastAPI()
    context = ExecutionContext(deadline=datetime.now(UTC) - timedelta(seconds=1))

    @app.middleware("http")
    async def expired(request, call_next):  # type: ignore[no-untyped-def]
        with use_context(context):
            return await call_next(request)

    app.include_router(create_graphql_router(application.tasks), prefix="/graphql")
    with TestClient(app) as http:
        response = http.post(
            "/graphql", json={"query": 'mutation { createTask(input:{prompt:"hello"}) { id } }'}
        )
        assert response.json()["errors"][0]["extensions"]["code"] == "OPERATION_TIMEOUT"
    assert application.tasks.list() == []


def test_shared_task_writes_reject_expired_and_cancelled_context(application):  # type: ignore[no-untyped-def]
    task = application.tasks.create("hello")
    service = application.container.resolve(TaskController)
    for context in (
        ExecutionContext(deadline=datetime.now(UTC) - timedelta(seconds=1)),
        ExecutionContext(),
    ):
        if context.deadline is None:
            context.cancellation.cancel()
        with use_context(context), pytest.raises(TaskOperationCancelled):
            service.delete(task.id)
        assert application.tasks.get(task.id) == task
