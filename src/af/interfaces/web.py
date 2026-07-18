"""Classic HTML MVC adapter with Jinja views."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from af.application.tasks import TaskController
from af.framework.types import PROMPT_MAX_LENGTH

TEMPLATE_DIRECTORY = Path(__file__).parent.parent / "views" / "templates"
CSRF_COOKIE_NAME = "__Host-af-csrf"
CSRF_FORM_FIELD = "csrf_token"
_CSRF_SECRET = secrets.token_bytes(32)


def _validate_prompt(prompt: str) -> str | None:
    """Return an HTML-friendly validation error message, or `None` when valid."""
    if not prompt.strip():
        return "prompt must not be blank"
    if len(prompt) > PROMPT_MAX_LENGTH:
        return f"prompt must be at most {PROMPT_MAX_LENGTH} characters"
    return None


def _issue_csrf_token() -> str:
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(
        _CSRF_SECRET,
        nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{signature}"


def _has_valid_csrf_signature(token: str) -> bool:
    nonce, separator, signature = token.partition(".")
    if not nonce or not separator or not signature:
        return False
    expected = hmac.new(
        _CSRF_SECRET,
        nonce.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _csrf_token_for_request(request: Request) -> str:
    token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if _has_valid_csrf_signature(token):
        return token
    return _issue_csrf_token()


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        path="/",
    )


def _same_origin(request: Request, header_value: str) -> bool:
    parsed = urlsplit(header_value)
    if not parsed.scheme or not parsed.netloc:
        return False
    return f"{parsed.scheme}://{parsed.netloc}" == f"{request.url.scheme}://{request.url.netloc}"


def _validate_csrf(request: Request, form_token: str) -> bool:
    if origin := request.headers.get("origin"):
        if not _same_origin(request, origin):
            return False
    elif (referer := request.headers.get("referer")) and not _same_origin(request, referer):
        return False
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not _has_valid_csrf_signature(cookie_token):
        return False
    if not _has_valid_csrf_signature(form_token):
        return False
    return hmac.compare_digest(cookie_token, form_token)


def _csrf_forbidden() -> HTMLResponse:
    return HTMLResponse("Forbidden", status_code=status.HTTP_403_FORBIDDEN)


def create_web_router(controller: TaskController) -> APIRouter:
    router = APIRouter(tags=["web"])
    templates = Jinja2Templates(directory=TEMPLATE_DIRECTORY)

    def render_index(
        request: Request,
        prompt: str = "how many rs in strawberry",
        *,
        error: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        token = _csrf_token_for_request(request)
        response = templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "tasks": controller.list(),
                "prompt": prompt,
                "error": error,
                "csrf_token": token,
            },
            status_code=status_code,
        )
        _set_csrf_cookie(response, token)
        return response

    def render_task(
        request: Request,
        task_id: str,
        *,
        prompt: str | None = None,
        error: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        task = controller.get(task_id)
        token = _csrf_token_for_request(request)
        response = templates.TemplateResponse(
            request=request,
            name="tasks/show.html",
            context={
                "task": task,
                "prompt": prompt or task.prompt,
                "error": error,
                "csrf_token": token,
            },
            status_code=status_code,
        )
        _set_csrf_cookie(response, token)
        return response

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return render_index(request)

    @router.get("/tasks", response_class=HTMLResponse)
    def list_tasks(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="tasks/index.html",
            context={"tasks": controller.list()},
        )

    @router.post("/tasks")
    def create_task(
        request: Request,
        prompt: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ) -> Response:
        if not _validate_csrf(request, csrf_token):
            return _csrf_forbidden()
        if error := _validate_prompt(prompt):
            return render_index(
                request,
                prompt,
                error=error,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        task = controller.create(prompt)
        return RedirectResponse(
            url=f"/tasks/{task.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.get("/tasks/{task_id}", response_class=HTMLResponse)
    def show_task(request: Request, task_id: str) -> HTMLResponse:
        return render_task(request, task_id)

    @router.post("/tasks/{task_id}")
    def update_task(
        request: Request,
        task_id: str,
        prompt: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ) -> Response:
        if not _validate_csrf(request, csrf_token):
            return _csrf_forbidden()
        if error := _validate_prompt(prompt):
            return render_task(
                request,
                task_id,
                prompt=prompt,
                error=error,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        task = controller.update(task_id, prompt)
        return RedirectResponse(
            url=f"/tasks/{task.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post("/tasks/{task_id}/delete")
    def delete_task(
        request: Request,
        task_id: str,
        csrf_token: str = Form(default=""),
    ) -> Response:
        if not _validate_csrf(request, csrf_token):
            return _csrf_forbidden()
        controller.delete(task_id)
        return RedirectResponse(url="/tasks", status_code=status.HTTP_303_SEE_OTHER)

    return router
