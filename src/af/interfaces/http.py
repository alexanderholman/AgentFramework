"""FastAPI HTTP kernel assembling HTML, REST, and GraphQL adapters."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from af.bootstrap import AFApplication, create_application
from af.framework.errors import InvalidPrompt, TaskNotFound, UnsupportedWork, VerificationFailed
from af.interfaces.graphql import create_graphql_router
from af.interfaces.rest import create_rest_router
from af.interfaces.web import create_web_router


def create_http_app(application: AFApplication | None = None) -> FastAPI:
    resolved = application or create_application()
    app = FastAPI(
        title=resolved.settings.app_name,
        version="0.1.0",
        description="Classification-heavy, inference-light AF reference application",
    )
    app.state.af = resolved
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
