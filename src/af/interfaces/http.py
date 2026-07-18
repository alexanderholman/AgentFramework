"""FastAPI HTTP kernel assembling HTML, REST, and GraphQL adapters."""

from __future__ import annotations

from collections.abc import Sequence
from ipaddress import ip_address

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
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
    RequestBodyTooLarge,
    read_bounded_receive,
    replay_receive,
    validate_content_length_header,
)
from af.interfaces.rest import create_rest_router
from af.interfaces.web import create_web_router


def _trusted_hosts_for(host: str, configured_hosts: Sequence[str] = ()) -> list[str]:
    explicit_hosts = {
        normalized_host
        for configured_host in configured_hosts
        for normalized_host in _normalize_trusted_host(configured_host)
    }
    if explicit_hosts:
        return sorted(explicit_hosts)
    if _is_loopback_host(host):
        return ["127.0.0.1", "::1", "[::1]", "localhost"]
    raise InvalidProviderConfiguration(
        "non-loopback HTTP bind requires at least one explicit allowed host"
    )


def _normalize_trusted_host(host: str) -> tuple[str, ...]:
    normalized = host.strip()
    if not normalized or "*" in normalized or "/" in normalized:
        raise InvalidProviderConfiguration("allowed host is invalid")
    try:
        parsed = ip_address(normalized.strip("[]"))
    except ValueError:
        return (normalized,)
    if parsed.version == 6:
        canonical = parsed.compressed
        return (canonical, f"[{canonical}]")
    return (parsed.compressed,)


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip()
    if normalized == "localhost":
        return True
    try:
        return ip_address(normalized.strip("[]")).is_loopback
    except ValueError:
        return False


class _BoundedBodyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1"): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        try:
            validate_content_length_header(headers.get("content-length"))
            body = await read_bounded_receive(receive)
        except InvalidContentLengthHeader:
            await self._send_error_response(
                scope,
                receive,
                send,
                status_code=status.HTTP_400_BAD_REQUEST,
                message="invalid Content-Length header",
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
        TrustedHostMiddleware,
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
