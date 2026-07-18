from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from graphql import GraphQLError, parse
from starlette.requests import Request

from af.domain.tasks import Task
from af.framework.errors import InvalidPrompt
from af.interfaces.graphql import (
    GRAPHQL_BAD_REQUEST_CODE,
    GRAPHQL_INTERNAL_ERROR_CODE,
    GRAPHQL_INVALID_DOCUMENT_MESSAGE,
    GRAPHQL_MAX_TASK_NODES,
    GRAPHQL_QUERY_TOO_COMPLEX_CODE,
    GRAPHQL_REQUEST_TOO_LARGE_CODE,
    _count_aliases,
    _count_root_fields,
    _count_task_nodes,
    _format_error,
    _GraphQLRequestError,
    _parse_query_document,
    _parse_request_payload,
    _read_bounded_body,
    _run_graphql_operation,
    _select_operation,
    _selection_depth,
    _validate_document_complexity,
    _validate_get_operation,
    create_graphql_router,
)


def _request(
    *,
    method: str,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    query_string: bytes = b"",
) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": "/graphql",
        "raw_path": b"/graphql",
        "query_string": query_string,
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }
    return Request(scope, receive)


class _Controller:
    def execute(self, prompt: str):  # type: ignore[no-untyped-def]
        return type("Outcome", (), {"output": "Hello, world!"})()

    def get(self, task_id: str) -> Task:
        return Task(
            id=task_id,
            prompt="how many rs in strawberry",
            output="3",
            intent="text.character_count",
            route="character-count",
            tier="procedural",
            status="completed",
            verified=True,
            reused=False,
            trace_id="trace-1",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def list(self, *, limit=None, offset=0):  # type: ignore[no-untyped-def]
        return []


@pytest.mark.anyio
async def test_run_graphql_operation_translates_expected_and_unexpected_errors() -> None:
    async def ok() -> str:
        return "ok"

    async def expected() -> str:
        raise InvalidPrompt("prompt must not be blank")

    async def unexpected() -> str:
        raise RuntimeError("SECRET")

    assert await _run_graphql_operation(ok) == "ok"

    with pytest.raises(GraphQLError, match="prompt must not be blank"):
        await _run_graphql_operation(expected)

    with pytest.raises(GraphQLError, match="internal GraphQL adapter error"):
        await _run_graphql_operation(unexpected)


def test_graphql_get_requests_read_only_task_fields() -> None:
    app = FastAPI()
    app.include_router(create_graphql_router(_Controller()), prefix="/graphql")

    with TestClient(app) as client:
        listed = client.get("/graphql", params={"query": "{ tasks(limit: 1, offset: 0) { id } }"})
        task = client.get(
            "/graphql",
            params={
                "query": "query Task($id: ID!) { task(id: $id) { id output } }",
                "variables": json.dumps({"id": "task-1"}),
                "operationName": "Task",
            },
        )

    assert listed.json() == {"data": {"tasks": []}}
    assert task.json() == {"data": {"task": {"id": "task-1", "output": "3"}}}


def test_graphql_complexity_helpers_cover_fragments_and_limits() -> None:
    document = parse(
        """
        query Example {
          ...TaskFields
          ... on Query {
            task(id: "task-1") { id }
          }
        }

        fragment TaskFields on Query {
          first: tasks(limit: 1, offset: 0) { id }
          second: tasks(limit: 1, offset: 1) { id }
        }
        """
    )
    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if definition.kind == "fragment_definition"
    }
    operation = next(
        definition
        for definition in document.definitions
        if definition.kind == "operation_definition"
    )
    selections = operation.selection_set.selections

    assert _count_aliases(selections, fragments) == 2
    assert _count_root_fields(selections, fragments) == 3
    assert _count_task_nodes(selections, fragments) == 2
    assert _selection_depth(selections, fragments, depth=1) == 2
    document_from_helper = _parse_query_document('{ task(id: "task-1") { id } }')
    operation = _select_operation(document_from_helper, None)
    assert operation.name is None
    assert getattr(operation.operation, "value", operation.operation) == "query"
    _validate_document_complexity(document)

    alias_bomb = " ".join(f"a{i}: tasks(limit: 100, offset: 0) {{ id }}" for i in range(1000))
    with pytest.raises(_GraphQLRequestError) as error_info:
        _parse_query_document(f"{{ {alias_bomb} }}")
    assert error_info.value.code == GRAPHQL_QUERY_TOO_COMPLEX_CODE

    with pytest.raises(Exception, match=GRAPHQL_INVALID_DOCUMENT_MESSAGE):
        _parse_query_document('{ task(id: "task-1") ')


def test_graphql_complexity_rejects_excessive_task_nodes() -> None:
    aliases = " ".join(f"a{i}: tasks(limit: 100, offset: 0) {{ prompt output }}" for i in range(3))

    with pytest.raises(_GraphQLRequestError, match="complexity limits") as error_info:
        _parse_query_document(f"{{ {aliases} }}")

    assert error_info.value.code == GRAPHQL_QUERY_TOO_COMPLEX_CODE
    assert GRAPHQL_MAX_TASK_NODES < 300


@pytest.mark.anyio
async def test_graphql_request_payload_validation_paths() -> None:
    get_request = _request(
        method="GET",
        query_string=(b"query=%7B%20hello%20%7D&variables=%7B%22id%22%3A%20%22task-1%22%7D"),
    )
    payload = await _parse_request_payload(get_request)
    assert payload.query == "{ hello }"
    assert payload.variables == {"id": "task-1"}

    with pytest.raises(Exception, match="No GraphQL query found"):
        await _parse_request_payload(_request(method="GET"))

    with pytest.raises(Exception, match="Unable to parse request body as JSON"):
        await _parse_request_payload(
            _request(method="GET", query_string=b"query=%7Bhello%7D&variables=not-json")
        )

    with pytest.raises(Exception, match="Unable to decode GraphQL query parameters"):
        await _parse_request_payload(_request(method="GET", query_string=b"query=%FF"))

    with pytest.raises(Exception, match="variables` must be an object"):
        await _parse_request_payload(
            _request(method="GET", query_string=b"query=%7Bhello%7D&variables=%5B1%5D")
        )

    deep_json = ("[" * 10_000) + "0" + ("]" * 10_000)
    with pytest.raises(Exception, match="Unable to parse request body as JSON"):
        await _parse_request_payload(
            _request(
                method="GET",
                query_string=f"query=%7Bhello%7D&variables={deep_json}".encode("ascii"),
            )
        )

    with pytest.raises(Exception, match="graphql request body must be at most 32768 bytes"):
        await _parse_request_payload(
            _request(
                method="GET",
                query_string=f"query={'x' * 32769}".encode("ascii"),
            )
        )

    with pytest.raises(Exception, match="Unsupported content type"):
        await _parse_request_payload(
            _request(method="POST", headers={"content-type": "text/plain"})
        )

    with pytest.raises(Exception, match="Unable to parse request body as JSON"):
        await _parse_request_payload(
            _request(method="POST", headers={"content-type": "application/json"}, body=b"{")
        )

    with pytest.raises(Exception, match="batch requests are not supported"):
        await _parse_request_payload(
            _request(
                method="POST",
                headers={"content-type": "application/json"},
                body=b"[]",
            )
        )

    with pytest.raises(Exception, match="`query` must be a string"):
        await _parse_request_payload(
            _request(
                method="POST",
                headers={"content-type": "application/json"},
                body=b'{"query": null}',
            )
        )

    with pytest.raises(Exception, match="`variables` must be an object"):
        await _parse_request_payload(
            _request(
                method="POST",
                headers={"content-type": "application/json"},
                body=b'{"query":"{ hello }","variables":[]}',
            )
        )

    with pytest.raises(Exception, match="`operationName` must be a string"):
        await _parse_request_payload(
            _request(
                method="POST",
                headers={"content-type": "application/json"},
                body=b'{"query":"{ hello }","operationName":1}',
            )
        )


@pytest.mark.anyio
async def test_graphql_body_limits_and_error_formatting() -> None:
    invalid_length = _request(
        method="POST",
        headers={"content-type": "application/json", "content-length": "bad"},
        body=b"{}",
    )
    with pytest.raises(_GraphQLRequestError) as error_info:
        await _read_bounded_body(invalid_length)
    assert error_info.value.code == GRAPHQL_BAD_REQUEST_CODE

    oversized = _request(
        method="POST",
        headers={"content-type": "application/json", "content-length": "40000"},
        body=b"{}",
    )
    with pytest.raises(_GraphQLRequestError) as error_info:
        await _read_bounded_body(oversized)
    assert error_info.value.code == GRAPHQL_REQUEST_TOO_LARGE_CODE

    unexpected = GraphQLError("SECRET", original_error=RuntimeError("SECRET"))
    formatted = _format_error(unexpected)
    assert formatted["message"] == "internal GraphQL adapter error"
    assert formatted["extensions"] == {"code": GRAPHQL_INTERNAL_ERROR_CODE}

    expected = GraphQLError("prompt must not be blank", extensions={"code": "INVALIDPROMPT"})
    assert _format_error(expected) == expected.formatted


def test_graphql_operation_selection_rejects_invalid_get_operation_shapes() -> None:
    with pytest.raises(_GraphQLRequestError, match="operationName is required"):
        _select_operation(parse("query First { hello } query Second { hello }"), None)

    with pytest.raises(_GraphQLRequestError, match="unknown GraphQL operation"):
        _select_operation(parse("query Known { hello }"), "Missing")

    with pytest.raises(_GraphQLRequestError, match="define an operation"):
        _select_operation(parse("fragment Example on Query { hello }"), None)

    with pytest.raises(
        _GraphQLRequestError, match="GET requests may only execute query operations"
    ):
        _validate_get_operation(
            parse('mutation Create { createTask(input: {prompt: "x"}) { id } }'), None
        )
