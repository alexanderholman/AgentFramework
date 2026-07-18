"""REST Task CRUD adapter."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from af.application.tasks import TaskController
from af.domain.tasks import Task


class TaskPrompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=10_000)

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
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
    def from_task(cls, task: Task) -> TaskResponse:
        return cls.model_validate(task)


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int


def create_rest_router(controller: TaskController) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["tasks"])

    @router.get("/hello")
    def hello() -> dict[str, object]:
        outcome = controller.execute("hello")
        return {"output": outcome.output, "verified": outcome.verified}

    @router.get("/tasks", response_model=TaskListResponse)
    def list_tasks() -> TaskListResponse:
        items = [TaskResponse.from_task(task) for task in controller.list()]
        return TaskListResponse(items=items, total=len(items))

    @router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
    def create_task(payload: TaskPrompt, response: Response) -> TaskResponse:
        task = controller.create(payload.prompt)
        response.headers["Location"] = f"/api/v1/tasks/{task.id}"
        return TaskResponse.from_task(task)

    @router.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task(task_id: str) -> TaskResponse:
        return TaskResponse.from_task(controller.get(task_id))

    @router.patch("/tasks/{task_id}", response_model=TaskResponse)
    @router.put("/tasks/{task_id}", response_model=TaskResponse, include_in_schema=False)
    def update_task(task_id: str, payload: TaskPrompt) -> TaskResponse:
        return TaskResponse.from_task(controller.update(task_id, payload.prompt))

    @router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_task(task_id: str) -> Response:
        controller.delete(task_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
