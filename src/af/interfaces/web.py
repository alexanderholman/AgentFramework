"""Classic HTML MVC adapter with Jinja views."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from af.application.tasks import TaskController
from af.framework.types import PROMPT_MAX_LENGTH

TEMPLATE_DIRECTORY = Path(__file__).parent.parent / "views" / "templates"
CSRF_COOKIE_NAME = "af-csrf"
SESSION_COOKIE_NAME = "af-session"
CSRF_FORM_FIELD = "csrf_token"
_CSRF_SECRET = secrets.token_bytes(32)
_MAX_SESSION_COOKIE_BYTES = 1024

type WebProfile = Literal["basic", "pretty", "experimental"]


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


def _sign_session_payload(payload: bytes, *, signing_key: bytes) -> str:
    signature = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
    encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    return f"{encoded}.{signature}"


def _read_session(request: Request, *, signing_key: bytes) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE_NAME, "")
    encoded, separator, signature = token.partition(".")
    if not encoded or not separator or not signature:
        return {}
    if len(encoded) > _MAX_SESSION_COOKIE_BYTES:
        return {}
    try:
        payload = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        return {}
    expected = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return {}
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    session: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            session[key] = value
    return session


def _write_session(
    request: Request,
    response: Response,
    session: dict[str, str],
    *,
    signing_key: bytes,
) -> None:
    if not session:
        response.delete_cookie(
            key=SESSION_COOKIE_NAME,
            path="/",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="lax",
        )
        return
    payload = json.dumps(session, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_sign_session_payload(payload, signing_key=signing_key),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


def _flash_message(request: Request, *, signing_key: bytes) -> str | None:
    flash = _read_session(request, signing_key=signing_key).get("flash")
    return flash or None


def _set_flash(request: Request, response: Response, message: str, *, signing_key: bytes) -> None:
    _write_session(request, response, {"flash": message}, signing_key=signing_key)


def _clear_flash(request: Request, response: Response, *, signing_key: bytes) -> None:
    if request.cookies.get(SESSION_COOKIE_NAME):
        _write_session(request, response, {}, signing_key=signing_key)


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


def _set_csrf_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
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


def create_web_router(
    controller: TaskController,
    *,
    web_profile: WebProfile,
    experimental_state: Literal["disabled", "fallback", "enabled"],
    session_cookie_signing_key: str,
) -> APIRouter:
    router = APIRouter(tags=["web"])
    templates = Jinja2Templates(directory=TEMPLATE_DIRECTORY)
    signing_key = session_cookie_signing_key.encode("utf-8")

    def profile_context() -> dict[str, str]:
        return {
            "web_profile": web_profile,
            "experimental_state": experimental_state,
        }

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
                "flash": _flash_message(request, signing_key=signing_key),
                "csrf_token": token,
                **profile_context(),
            },
            status_code=status_code,
        )
        _set_csrf_cookie(request, response, token)
        _clear_flash(request, response, signing_key=signing_key)
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
                "flash": _flash_message(request, signing_key=signing_key),
                "csrf_token": token,
                **profile_context(),
            },
            status_code=status_code,
        )
        _set_csrf_cookie(request, response, token)
        _clear_flash(request, response, signing_key=signing_key)
        return response

    @router.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return render_index(request)

    @router.get("/tasks", response_class=HTMLResponse)
    def list_tasks(request: Request) -> HTMLResponse:
        response = templates.TemplateResponse(
            request=request,
            name="tasks/index.html",
            context={
                "tasks": controller.list(),
                "flash": _flash_message(request, signing_key=signing_key),
                **profile_context(),
            },
        )
        _clear_flash(request, response, signing_key=signing_key)
        return response

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
        response = RedirectResponse(
            url=f"/tasks/{task.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        _set_flash(request, response, "Saved task.", signing_key=signing_key)
        return response

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
        response = RedirectResponse(
            url=f"/tasks/{task.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )
        _set_flash(request, response, "Updated task.", signing_key=signing_key)
        return response

    @router.post("/tasks/{task_id}/delete")
    def delete_task(
        request: Request,
        task_id: str,
        csrf_token: str = Form(default=""),
    ) -> Response:
        if not _validate_csrf(request, csrf_token):
            return _csrf_forbidden()
        controller.delete(task_id)
        response = RedirectResponse(url="/tasks", status_code=status.HTTP_303_SEE_OTHER)
        _set_flash(request, response, "Deleted task.", signing_key=signing_key)
        return response

    return router
