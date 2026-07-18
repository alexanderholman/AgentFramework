"""Classic HTML MVC adapter with Jinja views."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from af.application.tasks import TaskController

TEMPLATE_DIRECTORY = Path(__file__).parent.parent / "views" / "templates"
PROMPT_MAX_LENGTH = 10_000


def _prompt_error(prompt: str) -> str | None:
    if not prompt.strip():
        return "prompt must not be blank"
    if len(prompt) > PROMPT_MAX_LENGTH:
        return f"prompt must be at most {PROMPT_MAX_LENGTH} characters"
    return None


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
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"tasks": controller.list(), "prompt": prompt, "error": error},
            status_code=status_code,
        )

    def render_task(
        request: Request,
        task_id: str,
        *,
        prompt: str | None = None,
        error: str | None = None,
        status_code: int = status.HTTP_200_OK,
    ) -> HTMLResponse:
        task = controller.get(task_id)
        return templates.TemplateResponse(
            request=request,
            name="tasks/show.html",
            context={"task": task, "prompt": prompt or task.prompt, "error": error},
            status_code=status_code,
        )

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
    def create_task(request: Request, prompt: str = Form(default="")) -> Response:
        if error := _prompt_error(prompt):
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
    ) -> Response:
        if error := _prompt_error(prompt):
            return render_task(
                request,
                task_id,
                prompt=prompt,
                error=error,
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            )
        controller.update(task_id, prompt)
        return RedirectResponse(
            url=f"/tasks/{task_id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    @router.post("/tasks/{task_id}/delete")
    def delete_task(task_id: str) -> RedirectResponse:
        controller.delete(task_id)
        return RedirectResponse(url="/tasks", status_code=status.HTTP_303_SEE_OTHER)

    return router
