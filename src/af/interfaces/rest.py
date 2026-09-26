"""REST Task CRUD adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from af.application.locales import LocaleSelection, Localizer
from af.application.operations import (
    LifecycleStateValue,
    LivenessValue,
    OperationsController,
    OperationsSnapshot,
    ReadinessValue,
)
from af.application.tasks import TaskController
from af.domain.tasks import Task
from af.framework.types import PROMPT_MAX_LENGTH
from af.interfaces.http_operations import expected_version, task_etag
from af.interfaces.locale_http import LocalePresentation, content_language, request_locale
from af.interfaces.locale_presenters import localize_outcome, localize_task


class TaskPrompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=PROMPT_MAX_LENGTH)

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
    presentation: LocalePresentation | None = None

    @classmethod
    def from_task(cls, task: Task) -> TaskResponse:
        return cls.model_validate(task)


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    total: int


class RuntimeResponse(BaseModel):
    lifecycle_state: LifecycleStateValue
    liveness: LivenessValue
    readiness: ReadinessValue
    reason: str


class TaskActivityResponse(BaseModel):
    available: bool
    window_limit: int = Field(ge=1, le=25)
    observed: int = Field(ge=0, le=25)
    by_status: dict[str, int]
    verified: int = Field(ge=0, le=25)
    reused: int = Field(ge=0, le=25)


class RecentTaskResponse(BaseModel):
    id: str
    status: str
    route: str
    tier: str
    verified: bool
    reused: bool
    created_at: datetime
    updated_at: datetime


class OperationsResponse(BaseModel):
    schema_version: Literal["1"]
    observed_at: datetime
    runtime: RuntimeResponse
    activity: TaskActivityResponse
    recent_tasks: list[RecentTaskResponse] = Field(max_length=10)

    @classmethod
    def from_snapshot(cls, snapshot: OperationsSnapshot) -> OperationsResponse:
        return cls(
            schema_version=snapshot.schema_version,
            observed_at=snapshot.observed_at,
            runtime=RuntimeResponse(
                lifecycle_state=snapshot.runtime.lifecycle_state,
                liveness=snapshot.runtime.liveness,
                readiness=snapshot.runtime.readiness,
                reason=snapshot.runtime.reason,
            ),
            activity=TaskActivityResponse(
                available=snapshot.activity.available,
                window_limit=snapshot.activity.window_limit,
                observed=snapshot.activity.observed,
                by_status=dict(snapshot.activity.by_status),
                verified=snapshot.activity.verified,
                reused=snapshot.activity.reused,
            ),
            recent_tasks=[
                RecentTaskResponse.model_validate(task, from_attributes=True)
                for task in snapshot.recent_tasks
            ],
        )


def create_rest_router(
    controller: TaskController,
    operations: OperationsController | None = None,
    *,
    localizer: Localizer | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["tasks"])

    def select_locale(request: Request, response: Response) -> LocaleSelection | None:
        selection = request_locale(request)
        if selection is not None:
            if localizer is None:
                raise HTTPException(400, detail="locale_unavailable")
            response.headers["Content-Language"] = content_language(localizer, selection)
        return selection

    selected_locale = Depends(select_locale)

    def present(task: Task, selection: LocaleSelection | None) -> TaskResponse:
        if localizer is not None and selection is not None:
            return TaskResponse.model_validate(localize_task(task, localizer, selection))
        return TaskResponse.from_task(task)

    @router.post("/hello")
    def hello(
        selection: LocaleSelection | None = selected_locale,
    ) -> dict[str, object]:
        outcome = controller.execute("hello")
        result: dict[str, object] = {"output": outcome.output, "verified": outcome.verified}
        if localizer is not None and selection is not None:
            result["presentation"] = localize_outcome(outcome, localizer, selection)["presentation"]
        return result

    if operations is not None:

        @router.get("/operations", response_model=OperationsResponse, tags=["operations"])
        def get_operations() -> OperationsResponse:
            return OperationsResponse.from_snapshot(operations.snapshot())

    @router.get("/tasks", response_model=TaskListResponse, response_model_exclude_unset=True)
    def list_tasks(
        selection: LocaleSelection | None = selected_locale,
    ) -> TaskListResponse:
        items = [present(task, selection) for task in controller.list()]
        return TaskListResponse(items=items, total=len(items))

    @router.post(
        "/tasks",
        response_model=TaskResponse,
        status_code=status.HTTP_201_CREATED,
        response_model_exclude_unset=True,
    )
    def create_task(
        payload: TaskPrompt,
        response: Response,
        selection: LocaleSelection | None = selected_locale,
    ) -> TaskResponse:
        task = controller.create(payload.prompt)
        response.headers["Location"] = f"/api/v1/tasks/{task.id}"
        response.headers["ETag"] = task_etag(task)
        return present(task, selection)

    @router.get("/tasks/{task_id}", response_model=TaskResponse, response_model_exclude_unset=True)
    def get_task(
        task_id: str,
        response: Response,
        selection: LocaleSelection | None = selected_locale,
    ) -> TaskResponse:
        task = controller.get(task_id)
        response.headers["ETag"] = task_etag(task)
        return present(task, selection)

    @router.patch(
        "/tasks/{task_id}", response_model=TaskResponse, response_model_exclude_unset=True
    )
    @router.put(
        "/tasks/{task_id}",
        response_model=TaskResponse,
        include_in_schema=False,
        response_model_exclude_unset=True,
    )
    def update_task(
        task_id: str,
        payload: TaskPrompt,
        request: Request,
        response: Response,
        selection: LocaleSelection | None = selected_locale,
    ) -> TaskResponse:
        task = controller.update(
            task_id, payload.prompt, expected_version=expected_version(request)
        )
        response.headers["ETag"] = task_etag(task)
        return present(task, selection)

    @router.delete(
        "/tasks/{task_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        dependencies=[Depends(select_locale)],
    )
    def delete_task(task_id: str, request: Request) -> Response:
        controller.delete(task_id, expected_version=expected_version(request))
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
