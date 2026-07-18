"""FastAPI HTTP kernel assembling HTML, REST, and GraphQL adapters."""

from __future__ import annotations

from ipaddress import ip_address

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from af import __version__
from af.bootstrap import AFApplication, create_application
from af.framework.errors import InvalidPrompt, TaskNotFound, UnsupportedWork, VerificationFailed
from af.interfaces.graphql import create_graphql_router
from af.interfaces.rest import create_rest_router
from af.interfaces.web import create_web_router


def _trusted_hosts_for(host: str) -> list[str]:
    normalized = host.strip()
    allowed_hosts = {normalized}
    if normalized == "localhost":
        allowed_hosts.update({"127.0.0.1", "[::1]", "::1"})
        return sorted(allowed_hosts)
    try:
        parsed = ip_address(normalized.strip("[]"))
    except ValueError:
        return sorted(allowed_hosts)
    if parsed.is_loopback:
        allowed_hosts.update({"127.0.0.1", "localhost", "[::1]", "::1"})
    return sorted(allowed_hosts)


def create_http_app(application: AFApplication | None = None) -> FastAPI:
    resolved = application or create_application()
    app = FastAPI(
        title=resolved.settings.app_name,
        version=__version__,
        description="Classification-heavy, inference-light AF reference application",
    )
    app.state.af = resolved
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=_trusted_hosts_for(resolved.settings.host),
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
