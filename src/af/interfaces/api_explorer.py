"""Locally served API documentation; no CDN, inline executable code, or policy bypass."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

_VIEWS = Path(__file__).parent.parent / "views"
_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def create_api_explorer_router(
    openapi: Callable[[], dict[str, Any]], graphql_schema: Callable[[], str]
) -> APIRouter:
    router = APIRouter(include_in_schema=False)
    templates = Jinja2Templates(directory=_VIEWS / "templates")

    @router.get("/docs", response_class=HTMLResponse)
    def documentation(request: Request) -> HTMLResponse:
        operations = []
        for path, methods in sorted(openapi().get("paths", {}).items()):
            if not path.startswith("/api/"):
                continue
            for method, operation in sorted(methods.items()):
                if method not in _METHODS:
                    continue
                content = operation.get("requestBody", {}).get("content", {})
                if content and "application/json" not in content:
                    continue
                operations.append(
                    {
                        "method": method.upper(),
                        "path": path,
                        "summary": operation.get("summary", ""),
                    }
                )
        return templates.TemplateResponse(
            request=request,
            name="api_explorer.html",
            context={"operations": operations, "kind": "rest"},
            headers={"Cache-Control": "no-store", "Content-Language": "en"},
        )

    @router.get("/redoc")
    def redoc() -> RedirectResponse:
        return RedirectResponse("/docs", status_code=307)

    @router.get("/graphql/explorer", response_class=HTMLResponse)
    def graphql(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="graphql_explorer.html",
            context={"kind": "graphql"},
            headers={"Cache-Control": "no-store", "Content-Language": "en"},
        )

    @router.get("/graphql/schema.graphql")
    def schema_document() -> PlainTextResponse:
        return PlainTextResponse(graphql_schema(), headers={"Cache-Control": "no-store"})

    @router.get("/assets/explorer.js")
    def javascript() -> FileResponse:
        return FileResponse(_VIEWS / "assets" / "explorer.js", media_type="text/javascript")

    @router.get("/assets/explorer.css")
    def stylesheet() -> FileResponse:
        return FileResponse(_VIEWS / "assets" / "explorer.css", media_type="text/css")

    return router
