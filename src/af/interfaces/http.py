"""FastAPI HTTP kernel assembling HTML, REST, and GraphQL adapters."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from af import __version__
from af.bootstrap import AFApplication, create_application
from af.framework.errors import (
    InvalidPrompt,
    InvalidProviderConfiguration,
    TaskNotFound,
    UnsupportedWork,
    VerificationFailed,
)
from af.interfaces.graphql import (
    GRAPHQL_BAD_REQUEST_CODE,
    GRAPHQL_REQUEST_TOO_LARGE_CODE,
    _graphql_error_response,
    _graphql_response_media_type,
    _GraphQLRequestError,
    create_graphql_router,
)
from af.interfaces.http_body import (
    BOUNDED_BODY_SCOPE_KEY,
    HTTP_MAX_BODY_BYTES,
    InvalidContentLengthHeader,
    InvalidRequestBody,
    RequestBodyTooLarge,
    content_length_headers_from_scope,
    read_bounded_receive,
    replay_receive,
    validate_content_length_header,
)
from af.interfaces.http_trust import format_host, host_matches, parse_host_value
from af.interfaces.rest import create_rest_router
from af.interfaces.web import create_web_router


def _scope_header_values(scope: Scope, header_name: str) -> tuple[str, ...]:
    normalized_header = header_name.lower()
    values: list[str] = []
    for key, value in scope.get("headers", []):
        if key.decode("latin-1").lower() == normalized_header:
            values.append(value.decode("latin-1"))
    return tuple(values)


def _trusted_hosts_for(host: str, configured_hosts: Sequence[str] = ()) -> list[str]:
    explicit_hosts = {
        format_host(parse_host_value(configured_host, error_message="allowed host is invalid"))
        for configured_host in configured_hosts
    }
    if explicit_hosts:
        return sorted(explicit_hosts)
    if _is_loopback_host(host):
        return ["127.0.0.1", "::1", "localhost"]
    raise InvalidProviderConfiguration(
        "non-loopback HTTP bind requires at least one explicit allowed host"
    )


def _is_loopback_host(host: str) -> bool:
    try:
        normalized = parse_host_value(host, error_message="host is invalid").host
    except InvalidProviderConfiguration:
        return False
    return normalized in {"127.0.0.1", "::1", "localhost"}


class _BoundedBodyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        header_values = content_length_headers_from_scope(scope.get("headers", []))
        try:
            expected_length = validate_content_length_header(header_values)
            body = await read_bounded_receive(receive, expected_length=expected_length)
        except InvalidContentLengthHeader:
            await self._send_error_response(
                scope,
                receive,
                send,
                status_code=status.HTTP_400_BAD_REQUEST,
                message="invalid Content-Length header",
            )
            return
        except InvalidRequestBody:
            await self._send_error_response(
                scope,
                receive,
                send,
                status_code=status.HTTP_400_BAD_REQUEST,
                message="invalid request body",
            )
            return
        except RequestBodyTooLarge:
            await self._send_error_response(
                scope,
                receive,
                send,
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                message=f"request body must be at most {HTTP_MAX_BODY_BYTES} bytes",
            )
            return
        scope[BOUNDED_BODY_SCOPE_KEY] = body
        await self._app(scope, replay_receive(body), send)

    async def _send_error_response(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        message: str,
    ) -> None:
        response: PlainTextResponse | JSONResponse
        path = scope.get("path", "")
        if path == "/graphql":
            try:
                media_type = _graphql_response_media_type(
                    {
                        key.decode("latin-1"): value.decode("latin-1")
                        for key, value in scope.get("headers", [])
                    }.get("accept")
                )
            except _GraphQLRequestError:
                media_type = "application/json"
            response = _graphql_error_response(
                (
                    f"graphql request body must be at most {HTTP_MAX_BODY_BYTES} bytes"
                    if status_code == status.HTTP_413_CONTENT_TOO_LARGE
                    else message
                ),
                (
                    GRAPHQL_REQUEST_TOO_LARGE_CODE
                    if status_code == status.HTTP_413_CONTENT_TOO_LARGE
                    else GRAPHQL_BAD_REQUEST_CODE
                ),
                status_code=status_code,
                media_type=media_type,
            )
        elif path.startswith("/api/v1"):
            response = JSONResponse(status_code=status_code, content={"detail": message})
        else:
            response = PlainTextResponse(message, status_code=status_code)
        await response(scope, receive, send)


class _TrustedHostMiddleware:
    def __init__(self, app: ASGIApp, *, allowed_hosts: Sequence[str]) -> None:
        self._app = app
        self._allowed_hosts = tuple(
            parse_host_value(host, error_message="allowed host is invalid")
            for host in allowed_hosts
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        host_headers = _scope_header_values(scope, "host")
        if len(host_headers) != 1:
            response = PlainTextResponse(
                "Invalid host header", status_code=status.HTTP_400_BAD_REQUEST
            )
            await response(scope, receive, send)
            return
        try:
            actual_host = parse_host_value(host_headers[0], error_message="host header is invalid")
        except InvalidProviderConfiguration:
            response = PlainTextResponse(
                "Invalid host header", status_code=status.HTTP_400_BAD_REQUEST
            )
            await response(scope, receive, send)
            return
        if not any(host_matches(allowed_host, actual_host) for allowed_host in self._allowed_hosts):
            response = PlainTextResponse(
                "Invalid host header", status_code=status.HTTP_400_BAD_REQUEST
            )
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def create_http_app(
    application: AFApplication | None = None,
    *,
    bind_host: str | None = None,
    allowed_hosts: Sequence[str] | None = None,
) -> FastAPI:
    resolved = application or create_application()
    resolved_bind_host = bind_host or resolved.settings.host
    resolved_allowed_hosts = (
        tuple(allowed_hosts) if allowed_hosts is not None else resolved.settings.allowed_hosts
    )
    app = FastAPI(
        title=resolved.settings.app_name,
        version=__version__,
        description="Classification-heavy, inference-light AF reference application",
    )
    app.state.af = resolved
    app.add_middleware(_BoundedBodyMiddleware)
    app.add_middleware(
        _TrustedHostMiddleware,
        allowed_hosts=_trusted_hosts_for(resolved_bind_host, resolved_allowed_hosts),
    )
    app.include_router(create_web_router(resolved.tasks))
    app.include_router(create_rest_router(resolved.tasks))
    app.include_router(create_graphql_router(resolved.tasks), prefix="/graphql")

    @app.exception_handler(TaskNotFound)
    async def task_not_found(_: Request, error: TaskNotFound) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": str(error)},
        )

    @app.exception_handler(InvalidPrompt)
    @app.exception_handler(UnsupportedWork)
    @app.exception_handler(VerificationFailed)
    async def unprocessable(_: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"detail": str(error)},
        )

    return app
