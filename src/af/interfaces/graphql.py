"""Strawberry GraphQL Task CRUD adapter."""

from __future__ import annotations

import json
import re
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
    ArgumentNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLSyntaxError,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    SelectionNode,
    parse,
)
from strawberry.types import Info

from af.application.tasks import TaskController
from af.domain.tasks import Task
from af.framework.errors import AFError, TaskNotFound
from af.framework.types import TASK_LIST_DEFAULT_LIMIT, TASK_LIST_MAX_LIMIT
from af.interfaces.http_body import (
    BOUNDED_BODY_SCOPE_KEY,
    HTTP_MAX_BODY_BYTES,
    InvalidContentLengthHeader,
    InvalidRequestBody,
    RequestBodyTooLarge,
    content_length_headers_from_scope,
    read_bounded_receive,
    validate_content_length_header,
)

GRAPHQL_INTERNAL_ERROR_CODE = "INTERNAL_ERROR"
GRAPHQL_INTERNAL_ERROR_MESSAGE = "internal GraphQL adapter error"
GRAPHQL_BAD_REQUEST_CODE = "BAD_REQUEST"
GRAPHQL_METHOD_NOT_ALLOWED_CODE = "METHOD_NOT_ALLOWED"
GRAPHQL_REQUEST_TOO_LARGE_CODE = "REQUEST_TOO_LARGE"
GRAPHQL_QUERY_TOO_COMPLEX_CODE = "QUERY_TOO_COMPLEX"
GRAPHQL_NOT_ACCEPTABLE_CODE = "NOT_ACCEPTABLE"
GRAPHQL_UNSUPPORTED_MEDIA_TYPE_CODE = "UNSUPPORTED_MEDIA_TYPE"
GRAPHQL_MAX_ALIASES = 50
GRAPHQL_MAX_DEPTH = 8
GRAPHQL_MAX_ROOT_FIELDS = 25
GRAPHQL_MAX_TASK_NODES = 200
GRAPHQL_MAX_TRAVERSAL_STEPS = 1_024
GRAPHQL_INVALID_DOCUMENT_MESSAGE = "Unable to parse GraphQL query document"
GRAPHQL_INVALID_QUERY_PARAMS_MESSAGE = "Unable to decode GraphQL query parameters"
GRAPHQL_INVALID_JSON_MESSAGE = "Unable to parse request body as JSON"
GRAPHQL_RESPONSE_MEDIA_TYPE = "application/graphql-response+json"
GRAPHQL_LEGACY_JSON_MEDIA_TYPE = "application/json"
_QVALUE = re.compile(r"^(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)$")
_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

GraphQLSelection = SelectionNode


@dataclass(frozen=True, slots=True)
class _GraphQLRequestPayload:
    query: str
    operation_name: str | None
    variables: dict[str, object] | None


class _GraphQLRequestError(Exception):
    def __init__(
        self,
        message: str,
        code: str,
        *,
        status_code: int,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.headers = dict(headers or {})


@dataclass(frozen=True, slots=True)
class _ParsedMediaValue:
    media_type: str
    parameters: dict[str, str]
    quality: float | None = None


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


def _graphql_response_media_type(accept_header: str | None) -> str:
    if accept_header is None or not accept_header.strip():
        return GRAPHQL_RESPONSE_MEDIA_TYPE
    supported = (GRAPHQL_RESPONSE_MEDIA_TYPE, GRAPHQL_LEGACY_JSON_MEDIA_TYPE)
    best_media_type: str | None = None
    best_rank: tuple[float, int] | None = None
    for media_type in supported:
        rank = _accept_rank(accept_header, media_type)
        if rank is None:
            continue
        if (
            best_rank is None
            or rank > best_rank
            or (rank == best_rank and media_type == GRAPHQL_RESPONSE_MEDIA_TYPE)
        ):
            best_media_type = media_type
            best_rank = rank
    if best_media_type is not None:
        return best_media_type
    raise _GraphQLRequestError(
        "Unsupported Accept header",
        GRAPHQL_NOT_ACCEPTABLE_CODE,
        status_code=status.HTTP_406_NOT_ACCEPTABLE,
    )


@strawberry.type
class Query:
    @strawberry.field
    async def tasks(
        self,
        info: Info[Any, Any],
        limit: int = TASK_LIST_DEFAULT_LIMIT,
        offset: int = 0,
    ) -> list[TaskType]:
        async def load_tasks() -> list[TaskType]:
            tasks = await run_in_threadpool(
                _controller(info).list,
                limit=limit,
                offset=offset,
            )
            return [TaskType.from_task(task) for task in tasks]

        return await _run_graphql_operation(load_tasks)

    @strawberry.field
    async def task(self, info: Info[Any, Any], id: strawberry.ID) -> TaskType | None:
        async def load_task() -> TaskType | None:
            try:
                task = await run_in_threadpool(_controller(info).get, str(id))
            except TaskNotFound:
                return None
            return TaskType.from_task(task)

        return await _run_graphql_operation(load_task)


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def hello(self, info: Info[Any, Any]) -> str:
        """Execute hello as a mutation because the shared pipeline records a trace."""
        outcome = await _run_graphql_operation(
            lambda: run_in_threadpool(_controller(info).execute, "hello")
        )
        return outcome.output

    @strawberry.mutation
    async def create_task(self, info: Info[Any, Any], input: TaskInput) -> TaskType:
        async def create() -> TaskType:
            task = await run_in_threadpool(_controller(info).create, input.prompt)
            return TaskType.from_task(task)

        return await _run_graphql_operation(create)

    @strawberry.mutation
    async def update_task(
        self,
        info: Info[Any, Any],
        id: strawberry.ID,
        input: TaskInput,
    ) -> TaskType:
        async def update() -> TaskType:
            task = await run_in_threadpool(
                _controller(info).update,
                str(id),
                input.prompt,
            )
            return TaskType.from_task(task)

        return await _run_graphql_operation(update)

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
    media_type: str = GRAPHQL_RESPONSE_MEDIA_TYPE,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"data": None, "errors": [{"message": message, "extensions": {"code": code}}]},
        media_type=media_type,
        headers=dict(headers or {}),
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


@dataclass(frozen=True, slots=True)
class _ComplexityMetrics:
    aliases: int
    depth: int
    root_fields: int
    task_nodes: int
    traversal_steps: int


def _task_limit_from_field(field: FieldNode) -> int:
    limit_argument: ArgumentNode | None = None
    for argument in field.arguments or ():
        if argument.name.value == "limit":
            limit_argument = argument
            break
    if limit_argument is None:
        return TASK_LIST_DEFAULT_LIMIT
    value = limit_argument.value
    if not isinstance(value, IntValueNode):
        return TASK_LIST_MAX_LIMIT
    try:
        parsed = int(value.value)
    except ValueError:
        return TASK_LIST_MAX_LIMIT
    if 1 <= parsed <= TASK_LIST_MAX_LIMIT:
        return parsed
    return TASK_LIST_MAX_LIMIT


def _complexity_error() -> _GraphQLRequestError:
    return _GraphQLRequestError(
        "graphql query exceeds the configured complexity limits",
        GRAPHQL_QUERY_TOO_COMPLEX_CODE,
        status_code=status.HTTP_200_OK,
    )


def _measure_complexity(
    selections: Sequence[GraphQLSelection],
    fragments: Mapping[str, FragmentDefinitionNode],
    *,
    depth: int,
) -> _ComplexityMetrics:
    aliases = 0
    max_depth = depth
    root_fields = 0
    task_nodes = 0
    traversal_steps = 0
    stack: list[tuple[GraphQLSelection, int, bool, frozenset[str]]] = [
        (selection, depth, True, frozenset()) for selection in reversed(selections)
    ]
    while stack:
        selection, current_depth, at_root, active_fragments = stack.pop()
        traversal_steps += 1
        if traversal_steps > GRAPHQL_MAX_TRAVERSAL_STEPS:
            raise _complexity_error()
        if isinstance(selection, FieldNode):
            max_depth = max(max_depth, current_depth)
            if selection.alias is not None:
                aliases += 1
            if at_root:
                root_fields += 1
            if selection.name.value == "tasks":
                task_nodes += _task_limit_from_field(selection)
            if (
                aliases > GRAPHQL_MAX_ALIASES
                or max_depth > GRAPHQL_MAX_DEPTH
                or root_fields > GRAPHQL_MAX_ROOT_FIELDS
                or task_nodes > GRAPHQL_MAX_TASK_NODES
            ):
                raise _complexity_error()
            if selection.selection_set is not None:
                stack.extend(
                    (nested, current_depth + 1, False, active_fragments)
                    for nested in reversed(selection.selection_set.selections)
                )
            continue
        if isinstance(selection, InlineFragmentNode):
            stack.extend(
                (nested, current_depth, at_root, active_fragments)
                for nested in reversed(selection.selection_set.selections)
            )
            continue
        if isinstance(selection, FragmentSpreadNode):
            fragment_name = selection.name.value
            if fragment_name in active_fragments:
                raise _complexity_error()
            fragment = fragments.get(fragment_name)
            if fragment is None:
                continue
            next_fragments = active_fragments | {fragment_name}
            stack.extend(
                (nested, current_depth, at_root, next_fragments)
                for nested in reversed(fragment.selection_set.selections)
            )
    return _ComplexityMetrics(
        aliases=aliases,
        depth=max_depth,
        root_fields=root_fields,
        task_nodes=task_nodes,
        traversal_steps=traversal_steps,
    )


def _selection_depth(
    selections: Sequence[GraphQLSelection],
    fragments: Mapping[str, FragmentDefinitionNode],
    *,
    depth: int,
    active_fragments: frozenset[str] = frozenset(),
) -> int:
    del active_fragments
    return _measure_complexity(selections, fragments, depth=depth).depth


def _count_aliases(
    selections: Sequence[GraphQLSelection],
    fragments: Mapping[str, FragmentDefinitionNode],
    *,
    active_fragments: frozenset[str] = frozenset(),
) -> int:
    del active_fragments
    return _measure_complexity(selections, fragments, depth=1).aliases


def _count_root_fields(
    selections: Sequence[GraphQLSelection],
    fragments: Mapping[str, FragmentDefinitionNode],
    *,
    active_fragments: frozenset[str] = frozenset(),
) -> int:
    del active_fragments
    return _measure_complexity(selections, fragments, depth=1).root_fields


def _count_task_nodes(
    selections: Sequence[GraphQLSelection],
    fragments: Mapping[str, FragmentDefinitionNode],
    *,
    active_fragments: frozenset[str] = frozenset(),
) -> int:
    del active_fragments
    return _measure_complexity(selections, fragments, depth=1).task_nodes


def _validate_document_complexity(document: DocumentNode) -> None:
    fragments = {
        definition.name.value: definition
        for definition in document.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    for definition in document.definitions:
        if isinstance(definition, OperationDefinitionNode):
            _measure_complexity(definition.selection_set.selections, fragments, depth=1)


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
            headers={"Allow": "POST"},
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
    if encoded_length > HTTP_MAX_BODY_BYTES:
        raise _GraphQLRequestError(
            f"graphql {label} must be at most {HTTP_MAX_BODY_BYTES} bytes",
            GRAPHQL_REQUEST_TOO_LARGE_CODE,
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        )


def _parse_get_parameters(request: Request) -> dict[str, str]:
    raw_query_string = request.scope.get("query_string", b"")
    if len(raw_query_string) > HTTP_MAX_BODY_BYTES:
        raise _GraphQLRequestError(
            f"graphql request body must be at most {HTTP_MAX_BODY_BYTES} bytes",
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
    cached_body = request.scope.get(BOUNDED_BODY_SCOPE_KEY)
    if isinstance(cached_body, bytes):
        return cached_body
    try:
        expected_length = validate_content_length_header(
            content_length_headers_from_scope(request.scope.get("headers", []))
        )
    except InvalidContentLengthHeader:
        raise _GraphQLRequestError(
            "invalid Content-Length header",
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from None
    except RequestBodyTooLarge:
        raise _GraphQLRequestError(
            f"graphql request body must be at most {HTTP_MAX_BODY_BYTES} bytes",
            GRAPHQL_REQUEST_TOO_LARGE_CODE,
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        ) from None
    try:
        return await read_bounded_receive(request.receive, expected_length=expected_length)
    except RequestBodyTooLarge:
        raise _GraphQLRequestError(
            f"graphql request body must be at most {HTTP_MAX_BODY_BYTES} bytes",
            GRAPHQL_REQUEST_TOO_LARGE_CODE,
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        ) from None
    except InvalidRequestBody:
        raise _GraphQLRequestError(
            "invalid request body",
            GRAPHQL_BAD_REQUEST_CODE,
            status_code=status.HTTP_400_BAD_REQUEST,
        ) from None


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

    if _parse_content_type(request.headers.get("content-type")) != "application/json":
        raise _GraphQLRequestError(
            "Unsupported content type",
            GRAPHQL_UNSUPPORTED_MEDIA_TYPE_CODE,
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
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
        media_type = _graphql_response_media_type(request.headers.get("accept"))
    except _GraphQLRequestError as error:
        return _graphql_error_response(
            error.message,
            error.code,
            status_code=error.status_code,
            media_type=GRAPHQL_LEGACY_JSON_MEDIA_TYPE,
        )
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
            media_type=media_type,
            headers=error.headers,
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
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=response_body,
        media_type=media_type,
    )


def create_graphql_router(controller: TaskController) -> APIRouter:
    router = APIRouter(tags=["graphql"])

    @router.get("")
    async def graphql_get(request: Request) -> JSONResponse:
        return await _handle_graphql_request(request, controller)

    @router.post("")
    async def graphql_post(request: Request) -> JSONResponse:
        return await _handle_graphql_request(request, controller)

    return router


def _accept_rank(accept_header: str, media_type: str) -> tuple[float, int] | None:
    best_match: tuple[int, int, float] | None = None
    for part in _split_header_values(
        accept_header,
        delimiter=",",
        message="Unsupported Accept header",
        code=GRAPHQL_NOT_ACCEPTABLE_CODE,
        status_code=status.HTTP_406_NOT_ACCEPTABLE,
    ):
        parsed = _parse_media_type(
            part,
            message="Unsupported Accept header",
            code=GRAPHQL_NOT_ACCEPTABLE_CODE,
            status_code=status.HTTP_406_NOT_ACCEPTABLE,
            allow_quality=True,
        )
        quality = parsed.quality if parsed.quality is not None else 1.0
        specificity = _media_specificity(parsed.media_type, media_type)
        if specificity < 0 or parsed.parameters:
            continue
        match = (specificity, 0, quality)
        if (
            best_match is None
            or match[:2] > best_match[:2]
            or (match[:2] == best_match[:2] and match[2] > best_match[2])
        ):
            best_match = match
    if best_match is None or best_match[2] == 0:
        return None
    return (best_match[2], best_match[0])


def _parse_content_type(content_type_header: str | None) -> str | None:
    if content_type_header is None or not content_type_header.strip():
        return None
    parsed = _parse_media_type(
        content_type_header.strip(),
        message="Unsupported content type",
        code=GRAPHQL_UNSUPPORTED_MEDIA_TYPE_CODE,
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    )
    return parsed.media_type


def _parse_media_type(
    value: str,
    *,
    message: str,
    code: str,
    status_code: int,
    allow_quality: bool = False,
) -> _ParsedMediaValue:
    index = 0
    length = len(value)

    def invalid() -> _GraphQLRequestError:
        return _GraphQLRequestError(message, code, status_code=status_code)

    def skip_optional_whitespace(position: int) -> int:
        while position < length and value[position] in " \t":
            position += 1
        return position

    index = skip_optional_whitespace(index)
    type_start = index
    while index < length and _is_token_char(value[index]):
        index += 1
    if index == type_start or index >= length or value[index] != "/":
        raise invalid()
    major_type = value[type_start:index].lower()
    index += 1
    subtype_start = index
    while index < length and _is_token_char(value[index]):
        index += 1
    if index == subtype_start:
        raise invalid()
    subtype = value[subtype_start:index].lower()
    media_type = f"{major_type}/{subtype}"
    index = skip_optional_whitespace(index)
    if index < length and value[index] != ";":
        raise invalid()

    parameters: dict[str, str] = {}
    quality: float | None = None
    seen_quality = False
    while index < length:
        index += 1
        index = skip_optional_whitespace(index)
        name_start = index
        while index < length and _is_token_char(value[index]):
            index += 1
        if index == name_start:
            raise invalid()
        parameter_name = value[name_start:index].lower()
        if parameter_name in parameters or (parameter_name == "q" and seen_quality):
            raise invalid()
        index = skip_optional_whitespace(index)
        if index >= length or value[index] != "=":
            raise invalid()
        index += 1
        index = skip_optional_whitespace(index)
        parameter_value, index = _parse_parameter_value(value, index, invalid)
        index = skip_optional_whitespace(index)
        if index < length and value[index] != ";":
            raise invalid()
        if allow_quality and parameter_name == "q":
            quality = _parse_quality(parameter_value)
            seen_quality = True
            continue
        parameters[parameter_name] = parameter_value
    return _ParsedMediaValue(
        media_type=media_type,
        parameters=parameters,
        quality=quality if allow_quality else None,
    )


def _parse_quality(quality: str) -> float:
    if not _QVALUE.fullmatch(quality):
        raise _GraphQLRequestError(
            "Unsupported Accept header",
            GRAPHQL_NOT_ACCEPTABLE_CODE,
            status_code=status.HTTP_406_NOT_ACCEPTABLE,
        )
    return float(quality)


def _parse_parameter_value(
    value: str,
    index: int,
    invalid: Callable[[], _GraphQLRequestError],
) -> tuple[str, int]:
    if index >= len(value):
        raise invalid()
    if value[index] == '"':
        return _parse_quoted_string(value, index, invalid)
    start = index
    while index < len(value) and _is_token_char(value[index]):
        index += 1
    if index == start:
        raise invalid()
    return value[start:index], index


def _parse_quoted_string(
    value: str,
    index: int,
    invalid: Callable[[], _GraphQLRequestError],
) -> tuple[str, int]:
    index += 1
    parsed: list[str] = []
    while index < len(value):
        character = value[index]
        codepoint = ord(character)
        if character == '"':
            return ("".join(parsed), index + 1)
        if character == "\\":
            index += 1
            if index >= len(value):
                raise invalid()
            escaped = value[index]
            if _is_control_char(escaped):
                raise invalid()
            parsed.append(escaped)
            index += 1
            continue
        if codepoint < 0x20 or codepoint == 0x7F:
            raise invalid()
        parsed.append(character)
        index += 1
    raise invalid()


def _split_header_values(
    value: str,
    *,
    delimiter: str,
    message: str,
    code: str,
    status_code: int,
) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False
    for character in value:
        if _is_control_char(character):
            raise _GraphQLRequestError(message, code, status_code=status_code)
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\" and in_quotes:
            current.append(character)
            escaped = True
            continue
        if character == '"':
            current.append(character)
            in_quotes = not in_quotes
            continue
        if character == delimiter and not in_quotes:
            part = "".join(current).strip()
            if not part:
                raise _GraphQLRequestError(message, code, status_code=status_code)
            parts.append(part)
            current = []
            continue
        current.append(character)
    if in_quotes or escaped:
        raise _GraphQLRequestError(message, code, status_code=status_code)
    part = "".join(current).strip()
    if not part:
        raise _GraphQLRequestError(message, code, status_code=status_code)
    parts.append(part)
    return parts


def _is_control_char(character: str) -> bool:
    codepoint = ord(character)
    return codepoint < 0x20 or codepoint == 0x7F


def _is_token_char(character: str) -> bool:
    return bool(_TOKEN.fullmatch(character))


def _media_specificity(accepted_media_type: str, media_type: str) -> int:
    if accepted_media_type == media_type:
        return 2
    if accepted_media_type == "*/*":
        return 0
    accepted_type, accepted_subtype = accepted_media_type.split("/", 1)
    media_type_name, _media_subtype = media_type.split("/", 1)
    if accepted_type == media_type_name and accepted_subtype == "*":
        return 1
    return -1
