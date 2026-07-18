"""Strawberry GraphQL Task CRUD adapter."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from urllib.parse import parse_qsl

import strawberry
from fastapi import APIRouter, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLSyntaxError,
    InlineFragmentNode,
    OperationDefinitionNode,
    SelectionNode,
    parse,
)
from strawberry.types import Info

from af.application.tasks import TaskController
from af.domain.tasks import Task
from af.framework.errors import AFError, TaskNotFound
from af.framework.types import TASK_LIST_DEFAULT_LIMIT

GRAPHQL_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
GRAPHQL_INTERNAL_ERROR_MESSAGE = "internal GraphQL adapter error"
GRAPHQL_BAD_REQUEST_CODE = "BAD_REQUEST"
GRAPHQL_METHOD_NOT_ALLOWED_CODE = "METHOD_NOT_ALLOWED"
GRAPHQL_REQUEST_TOO_LARGE_CODE = "REQUEST_TOO_LARGE"
GRAPHQL_QUERY_TOO_COMPLEX_CODE = "QUERY_TOO_COMPLEX"
GRAPHQL_MAX_BODY_BYTES = 32_768
GRAPHQL_MAX_ALIASES = 50
GRAPHQL_MAX_DEPTH = 8
GRAPHQL_MAX_ROOT_FIELDS = 25
GRAPHQL_INVALID_DOCUMENT_MESSAGE = "Unable to parse GraphQL query document"
GRAPHQL_INVALID_QUERY_PARAMS_MESSAGE = "Unable to decode GraphQL query parameters"
GRAPHQL_INVALID_JSON_MESSAGE = "Unable to parse request body as JSON"

GraphQLSelection = SelectionNode


@dataclass(frozen=True, slots=True)
class _GraphQLRequestPayload:
    query: str
    operation_name: str | None
    variables: dict[str, object] | None


class _GraphQLRequestError(Exception):
    def __init__(self, message: str, code: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


@strawberry.type
class TaskType:
    id: strawberry.ID
    prompt: str
    output: str
    intent: str
    route: str
    tier: str
    status: str
    verified: bool
    reused: bool
    trace_id: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_task(cls, task: Task) -> TaskType:
        return cls(
            id=strawberry.ID(task.id),
            prompt=task.prompt,
            output=task.output,
            intent=task.intent,
            route=task.route,
            tier=task.tier,
            status=task.status,
            verified=task.verified,
            reused=task.reused,
            trace_id=task.trace_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


@strawberry.input
class TaskInput:
    prompt: str


def _controller(info: Info[Any, Any]) -> TaskController:
    return cast(TaskController, info.context["controller"])


def _graphql_error(error: AFError) -> GraphQLError:
    return GraphQLError(
        str(error),
        extensions={"code": type(error).__name__.upper()},
    )


def _internal_graphql_error() -> GraphQLError:
    return GraphQLError(
        GRAPHQL_INTERNAL_ERROR_MESSAGE,
        extensions={"code": GRAPHQL_INTERNAL_ERROR_CODE},
    )


async def _run_graphql_operation[T](operation: Callable[[], Awaitable[T]]) -> T:
    af_error: AFError | None = None
    unexpected_failure = False
    try:
        return await operation()
    except AFError as error:
        af_error = error
    except Exception:
        unexpected_failure = True
    if af_error is not None:
        raise _graphql_error(af_error)
    if unexpected_failure:
        raise _internal_graphql_error()
    raise AssertionError("unreachable")


@strawberry.type
class Query:
    @strawberry.field
    async def tasks(
        self,
        info: Info[Any, Any],
        limit: int = TASK_LIST_DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[TaskType]:
        tasks = await _run_graphql_operation(
            lambda: run_in_threadpool(
                _controller(info).list,
                limit=limit,
                offset=offset,
            )
        )
        return [TaskType.from_task(task) for task in tasks]

    @strawberry.field
    async def task(self, info: Info[Any, Any], id: strawberry.ID) -> TaskType | None:
        unexpected_failure = False
        try:
            task = await run_in_threadpool(_controller(info).get, str(id))
        except TaskNotFound:
            return None
        except Exception:
            unexpected_failure = True
            task = None
        if unexpected_failure:
            raise _internal_graphql_error()
        assert task is not None
        return TaskType.from_task(task)

    @strawberry.field
    async def hello(self, info: Info[Any, Any]) -> str:
        outcome = await _run_graphql_operation(
            lambda: run_in_threadpool(_controller(info).execute, "hello")
        )
        return outcome.output


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_task(self, info: Info[Any, Any], input: TaskInput) -> TaskType:
        task = await _run_graphql_operation(
            lambda: run_in_threadpool(_controller(info).create, input.prompt)
        )
        return TaskType.from_task(task)

    @strawberry.mutation
    async def update_task(
        self,
        info: Info[Any, Any],
        id: strawberry.ID,
        input: TaskInput,
    ) -> TaskType:
        task = await _run_graphql_operation(
            lambda: run_in_threadpool(
                _controller(info).update,
                str(id),
                input.prompt,
            )
        )
        return TaskType.from_task(task)

    @strawberry.mutation
    async def delete_task(self, info: Info[Any, Any], id: strawberry.ID) -> bool:
        await _run_graphql_operation(lambda: run_in_threadpool(_controller(info).delete, str(id)))
        return True


schema = strawberry.Schema(query=Query, mutation=Mutation)


def _graphql_error_response(
    message: str,
    code: str,
    *,
    status_code: int,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"data": None, "errors": [{"message": message, "extensions": {"code": code}}]},
    )


def _format_error(error: GraphQLError) -> dict[str, object]:
    if error.original_error is not None and not isinstance(
        error.original_error,
        (AFError, GraphQLError),
    ):
        formatted = dict(error.formatted)
        formatted["message"] = GRAPHQL_INTERNAL_ERROR_MESSAGE
        formatted["extensions"] = {"code": GRAPHQL_INTERNAL_ERROR_CODE}
        return formatted
    return dict(error.formatted)


def _selection_depth(
    selections: Sequence[GraphQLSelection],
    fragments: Mapping[str, FragmentDefinitionNode],
    *,
    depth: int,
    active_fragments: frozenset[str] = frozenset(),
) -> int:
    max_depth = depth
    for selection in selections:
        if isinstance(selection, FieldNode):
            if selection.selection_set is not None:
                max_depth = max(
                    max_depth,
                    _selection_depth(
                        selection.selection_set.selections,
                        fragments,
                        depth=depth + 1,
                        active_fragments=active_fragments,
                    ),
                )
        elif isinstance(selection, InlineFragmentNode):
            max_depth = max(
                max_depth,
                _selection_depth(
                    selection.selection_set.selections,
                    fragments,
                    depth=depth,
                    active_fragments=active_fragments,
                ),
            )
        elif isinstance(selection, FragmentSpreadNode):
            fragment_name = selection.name.value
            if fragment_name in active_fragments:
                continue
            fragment = fragments.get(fragment_name)
            if fragment is None:
                continue
            max_depth = max(
                max_depth,
                _selection_depth(
                    fragment.selection_set.selections,
                    fragments,
                    depth=depth,
                    active_fragments=active_fragments | {fragment_name},
                ),
            )
    return max_depth


def _count_aliases(
    selections: Sequence[GraphQLSelection],
    fragments: Mapping[str, FragmentDefinitionNode],
    *,
    active_fragments: frozenset[str] = frozenset(),
) -> int:
    aliases = 0
    for selection in selections:
        if isinstance(selection, FieldNode):
            if selection.alias is not None:
                aliases += 1
            if selection.selection_set is not None:
                aliases += _count_aliases(
                    selection.selection_set.selections,
                    fragments,
                    active_fragments=active_fragments,
                )
        elif isinstance(selection, InlineFragmentNode):
            aliases += _count_aliases(
                selection.selection_set.selections,
                fragments,
                active_fragments=active_fragments,
            )
        elif isinstance(selection, FragmentSpreadNode):
            fragment_name = selection.name.value
            if fragment_name in active_fragments:
                continue
            fragment = fragments.get(fragment_name)
            if fragment is None:
                continue
            aliases += _count_aliases(
                fragment.selection_set.selections,
                fragments,
                active_fragments=active_fragments | {fragment_name},
            )
    return aliases


def _count_root_fields(
    selections: Sequence[GraphQLSelection],
    fragments: Mapping[str, FragmentDefinitionNode],
    *,
    active_fragments: frozenset[str] = frozenset(),
) -> int:
    total = 0
    for selection in selections:
        if isinstance(selection, FieldNode):
            total += 1
        elif isinstance(selection, InlineFragmentNode):
            total += _count_root_fields(
                selection.selection_set.selections,
                fragments,
                active_fragments=active_fragments,
            )
        elif isinstance(selection, FragmentSpreadNode):
            fragment_name = selection.name.value
            if fragment_name in active_fragments:
                continue
            fragment = fragments.get(fragment_name)
            if fragment is None:
                continue
            total += _count_root_fields(
                fragment.selection_set.selections,
                fragments,
                active_fragments=active_fragments | {fragment_name},
            )
    return total


def _validate_document_complexity(document: DocumentNode) -> None:
    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    alias_count = 0
    max_depth = 0
    max_root_fields = 0
    for definition in document.definitions:
        if not isinstance(definition, OperationDefinitionNode):
            continue
        selections = definition.selection_set.selections
        alias_count += _count_aliases(selections, fragments)
        max_depth = max(max_depth, _selection_depth(selections, fragments, depth=1))
        max_root_fields = max(max_root_fields, _count_root_fields(selections, fragments))
    if (
        alias_count > GRAPHQL_MAX_ALIASES
        or max_depth > GRAPHQL_MAX_DEPTH
        or max_root_fields > GRAPHQL_MAX_ROOT_FIELDS
    ):
        raise _GraphQLRequestError(
            "graphql query exceeds the configured complexity limits",
            GRAPHQL_QUERY_TOO_COMPLEX_CODE,
            status_code=status.HTTP_200_OK,
        )


def _parse_query_document(query: str) -> DocumentNode:
    try:
        document = parse(query)
    except (GraphQLSyntaxError, RecursionError, ValueError):
        raise _GraphQLRequestError(
            GRAPHQL_INVALID_DOCUMENT_MESSAGE,
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from None
    _validate_document_complexity(document)
    return document


def _select_operation(
    document: DocumentNode,
    operation_name: str | None,
) -> OperationDefinitionNode:
    operations = [
        definition
        for definition in document.definitions
        if isinstance(definition, OperationDefinitionNode)
    ]
    if not operations:
        raise _GraphQLRequestError(
            "GraphQL document must define an operation",
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if operation_name is None:
        if len(operations) != 1:
            raise _GraphQLRequestError(
                "operationName is required when a document defines multiple operations",
                GRAPHQL_BAD_REQUEST_CODE,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return operations[0]
    for operation in operations:
        if operation.name is not None and operation.name.value == operation_name:
            return operation
    raise _GraphQLRequestError(
        "unknown GraphQL operation",
        GRAPHQL_BAD_REQUEST_CODE,
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _validate_get_operation(document: DocumentNode, operation_name: str | None) -> None:
    operation = _select_operation(document, operation_name)
    if getattr(operation.operation, "value", operation.operation) != "query":
        raise _GraphQLRequestError(
            "GET requests may only execute query operations",
            GRAPHQL_METHOD_NOT_ALLOWED_CODE,
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


def _validate_bounded_text(value: str, *, label: str) -> None:
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError:
        raise _GraphQLRequestError(
            GRAPHQL_INVALID_QUERY_PARAMS_MESSAGE,
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from None
    if encoded_length > GRAPHQL_MAX_BODY_BYTES:
        raise _GraphQLRequestError(
            f"graphql {label} must be at most {GRAPHQL_MAX_BODY_BYTES} bytes",
            GRAPHQL_REQUEST_TOO_LARGE_CODE,
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )


def _parse_get_parameters(request: Request) -> dict[str, str]:
    raw_query_string = request.scope.get("query_string", b"")
    if len(raw_query_string) > GRAPHQL_MAX_BODY_BYTES:
        raise _GraphQLRequestError(
            f"graphql request body must be at most {GRAPHQL_MAX_BODY_BYTES} bytes",
            GRAPHQL_REQUEST_TOO_LARGE_CODE,
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )
    try:
        pairs = parse_qsl(
            raw_query_string.decode("ascii"),
            keep_blank_values=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeDecodeError, ValueError):
        raise _GraphQLRequestError(
            GRAPHQL_INVALID_QUERY_PARAMS_MESSAGE,
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from None
    return {key: value for key, value in pairs}


async def _read_bounded_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > GRAPHQL_MAX_BODY_BYTES:
                raise _GraphQLRequestError(
                    f"graphql request body must be at most {GRAPHQL_MAX_BODY_BYTES} bytes",
                    GRAPHQL_REQUEST_TOO_LARGE_CODE,
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                )
        except ValueError:
            raise _GraphQLRequestError(
                "invalid Content-Length header",
                GRAPHQL_BAD_REQUEST_CODE,
                status_code=status.HTTP_400_BAD_REQUEST,
            ) from None
    size = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > GRAPHQL_MAX_BODY_BYTES:
            raise _GraphQLRequestError(
                f"graphql request body must be at most {GRAPHQL_MAX_BODY_BYTES} bytes",
                GRAPHQL_REQUEST_TOO_LARGE_CODE,
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _parse_request_payload(request: Request) -> _GraphQLRequestPayload:
    if request.method == "GET":
        params = _parse_get_parameters(request)
        query = params.get("query")
        operation_name = params.get("operationName")
        variables_param = params.get("variables")
        if query is None:
            raise _GraphQLRequestError(
                "No GraphQL query found in the request",
                GRAPHQL_BAD_REQUEST_CODE,
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        _validate_bounded_text(query, label="query")
        if operation_name is not None:
            _validate_bounded_text(operation_name, label="operationName")
        if variables_param is None:
            variables: dict[str, object] | None = None
        else:
            _validate_bounded_text(variables_param, label="variables")
            try:
                parsed_variables = json.loads(variables_param)
            except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError):
                raise _GraphQLRequestError(
                    GRAPHQL_INVALID_JSON_MESSAGE,
                    GRAPHQL_BAD_REQUEST_CODE,
                    status_code=status.HTTP_400_BAD_REQUEST,
                ) from None
            if parsed_variables is not None and not isinstance(parsed_variables, dict):
                raise _GraphQLRequestError(
                    "The GraphQL operation's `variables` must be an object or null, if provided.",
                    GRAPHQL_BAD_REQUEST_CODE,
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
            variables = cast(dict[str, object] | None, parsed_variables)
        return _GraphQLRequestPayload(
            query=query, operation_name=operation_name, variables=variables
        )

    if "application/json" not in (request.headers.get("content-type") or ""):
        raise _GraphQLRequestError(
            "Unsupported content type",
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    body = await _read_bounded_body(request)
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError):
        raise _GraphQLRequestError(
            GRAPHQL_INVALID_JSON_MESSAGE,
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from None
    if not isinstance(payload, dict):
        raise _GraphQLRequestError(
            "GraphQL batch requests are not supported",
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    query = payload.get("query")
    variables = payload.get("variables")
    operation_name = payload.get("operationName")
    if not isinstance(query, str):
        raise _GraphQLRequestError(
            "The GraphQL operation's `query` must be a string.",
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if variables is not None and not isinstance(variables, dict):
        raise _GraphQLRequestError(
            "The GraphQL operation's `variables` must be an object or null, if provided.",
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    if operation_name is not None and not isinstance(operation_name, str):
        raise _GraphQLRequestError(
            "The GraphQL operation's `operationName` must be a string or null, if provided.",
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    return _GraphQLRequestPayload(
        query=query,
        operation_name=operation_name,
        variables=variables,
    )


async def _handle_graphql_request(
    request: Request,
    controller: TaskController,
) -> JSONResponse:
    try:
        payload = await _parse_request_payload(request)
        document = _parse_query_document(payload.query)
        if request.method == "GET":
            _validate_get_operation(document, payload.operation_name)
    except _GraphQLRequestError as error:
        return _graphql_error_response(
            error.message,
            error.code,
            status_code=error.status_code,
        )
    result = await schema.execute(
        payload.query,
        variable_values=payload.variables,
        context_value={"controller": controller},
        operation_name=payload.operation_name,
    )
    response_body: dict[str, object] = {"data": result.data}
    if result.errors:
        response_body["errors"] = [_format_error(error) for error in result.errors]
    return JSONResponse(status_code=status.HTTP_200_OK, content=response_body)


def create_graphql_router(controller: TaskController) -> APIRouter:
    router = APIRouter(tags=["graphql"])

    @router.get("")
    async def graphql_get(request: Request) -> JSONResponse:
        return await _handle_graphql_request(request, controller)

    @router.post("")
    async def graphql_post(request: Request) -> JSONResponse:
        return await _handle_graphql_request(request, controller)

    return router
