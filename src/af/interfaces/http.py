"""FastAPI HTTP kernel assembling HTML, REST, and GraphQL adapters."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from typing import Literal, cast
from uuid import uuid4

from fastapi import FastAPI, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from af import __version__
from af.application.authentication import AuthenticationService
from af.application.workflow_control import WorkflowController
from af.bootstrap import AFApplication, create_application
from af.framework.authentication import AuthenticationDenied
from af.framework.container import InvocationKind
from af.framework.context import AcceptanceScope, ExecutionContext
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
from af.interfaces.ideons import create_ideon_router
from af.interfaces.rest import create_rest_router
from af.interfaces.web import WebProfile, create_web_router
from af.interfaces.workflows import create_workflow_router
from af.interfaces.xml_api import create_xml_router

_WEB_PROFILES = ("basic", "experimental", "pretty")


def _resolve_web_profile(
    web_profile: str,
    *,
    enable_experimental_web_features: bool,
) -> tuple[WebProfile, Literal["disabled", "fallback", "enabled"]]:
    if web_profile not in _WEB_PROFILES:
        allowed_profiles = ", ".join(_WEB_PROFILES)
        raise InvalidProviderConfiguration(
            f"web profile must be one of {allowed_profiles}; got {web_profile!r}"
        )
    if web_profile == "experimental":
        if enable_experimental_web_features:
            return "experimental", "enabled"
        return "pretty", "fallback"
    return cast(WebProfile, web_profile), "disabled"


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


class _RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, application: AFApplication) -> None:
        self._app = app
        self._application = application

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        principal = None
        headers = _scope_header_values(scope, "authorization")
        if self._application.settings.http_auth_required or headers:
            try:
                if (
                    len(headers) != 1
                    or not headers[0].startswith("Bearer ")
                    or len(headers[0]) > 1024
                ):
                    raise AuthenticationDenied()
                authentication = self._application.container.resolve(AuthenticationService)
                principal = await run_in_threadpool(
                    authentication.authenticate,
                    headers[0][7:],
                    tenant_id=self._application.settings.auth_tenant_id,
                    required_scopes=("af:read",)
                    if scope["method"] in {"GET", "HEAD", "OPTIONS"}
                    else ("af:write",),
                )
            except (AuthenticationDenied, ValueError):
                response = JSONResponse(
                    status_code=401,
                    content={"detail": "authentication required"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        context = ExecutionContext(
            principal=principal,
            tenant_id=self._application.settings.auth_tenant_id if principal else None,
            correlation_id=str(uuid4()),
            requested_scope=AcceptanceScope.DETERMINISTIC,
            effective_scope=AcceptanceScope.DETERMINISTIC,
        )
        async with self._application.container.invocation_scope(
            context=context,
            kind=InvocationKind.REQUEST,
        ) as invocation:
            state = scope.setdefault("state", {})
            state["af_invocation"] = invocation
            await self._app(scope, receive, send)


class _SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Frame-Options"] = "DENY"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "no-referrer"
                if scope.get("scheme") == "https":
                    headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
                elif "strict-transport-security" in headers:
                    del headers["strict-transport-security"]

                content_type = headers.get("content-type", "").split(";", maxsplit=1)[0].strip()
                if content_type == "text/html":
                    headers["Content-Security-Policy"] = (
                        "default-src 'self'; base-uri 'none'; form-action 'self'; "
                        "frame-ancestors 'none'"
                    )
                elif "content-security-policy" in headers:
                    del headers["content-security-policy"]
            await send(message)

        await self._app(scope, receive, send_with_security_headers)


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
    web_profile: str = "basic",
    enable_experimental_web_features: bool = False,
) -> FastAPI:
    owns_application = application is None
    resolved = application or create_application()
    try:
        resolved_bind_host = bind_host or resolved.settings.host
        resolved_allowed_hosts = (
            tuple(allowed_hosts) if allowed_hosts is not None else resolved.settings.allowed_hosts
        )
        resolved_web_profile, experimental_state = _resolve_web_profile(
            web_profile,
            enable_experimental_web_features=enable_experimental_web_features,
        )
        return _assemble_http_app(
            resolved,
            owns_application=owns_application,
            bind_host=resolved_bind_host,
            allowed_hosts=resolved_allowed_hosts,
            web_profile=resolved_web_profile,
            experimental_state=experimental_state,
        )
    except BaseException:
        if owns_application:
            with suppress(BaseException):
                resolved.shutdown()
        raise


def _assemble_http_app(
    resolved: AFApplication,
    *,
    owns_application: bool,
    bind_host: str,
    allowed_hosts: Sequence[str],
    web_profile: WebProfile,
    experimental_state: Literal["disabled", "fallback", "enabled"],
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owns_application:
                await resolved.ashutdown()

    app = FastAPI(
        title=resolved.settings.app_name,
        version=__version__,
        description="Classification-heavy, inference-light AF reference application",
        lifespan=lifespan,
    )
    app.state.af = resolved
    app.add_middleware(_RequestContextMiddleware, application=resolved)
    app.add_middleware(_BoundedBodyMiddleware)
    app.add_middleware(
        _TrustedHostMiddleware,
        allowed_hosts=_trusted_hosts_for(bind_host, allowed_hosts),
    )
    app.add_middleware(_SecurityHeadersMiddleware)
    app.include_router(
        create_web_router(
            resolved.tasks,
            web_profile=web_profile,
            experimental_state=experimental_state,
            session_cookie_signing_key=resolved.settings.session_cookie_signing_key,
            operations=resolved.operations,
        )
    )
    app.include_router(create_rest_router(resolved.tasks, resolved.operations))
    app.include_router(create_ideon_router(resolved.ideons))
    app.include_router(create_xml_router(resolved.tasks))
    app.include_router(create_workflow_router(resolved.container.resolve(WorkflowController)))
    app.include_router(
        create_graphql_router(
            resolved.tasks, workflows=resolved.container.resolve(WorkflowController)
        ),
        prefix="/graphql",
    )

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
