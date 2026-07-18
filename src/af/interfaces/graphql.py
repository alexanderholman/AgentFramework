"""Strawberry GraphQL Task CRUD adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

import strawberry
from fastapi.concurrency import run_in_threadpool
from graphql import GraphQLError
from strawberry.fastapi import GraphQLRouter
from strawberry.types import Info

from af.application.tasks import TaskController
from af.domain.tasks import Task
from af.framework.errors import AFError, TaskNotFound


@strawberry.type
class TaskType:
    id: strawberry.ID
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
    def from_task(cls, task: Task) -> TaskType:
        return cls(
            id=strawberry.ID(task.id),
            prompt=task.prompt,
            output=task.output,
            intent=task.intent,
            route=task.route,
            tier=task.tier,
            status=task.status,
            verified=task.verified,
            reused=task.reused,
            trace_id=task.trace_id,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )


@strawberry.input
class TaskInput:
    prompt: str


def _controller(info: Info[Any, Any]) -> TaskController:
    return cast(TaskController, info.context["controller"])


def _graphql_error(error: AFError) -> GraphQLError:
    return GraphQLError(
        str(error),
        extensions={"code": type(error).__name__.upper()},
    )


@strawberry.type
class Query:
    @strawberry.field
    async def tasks(self, info: Info[Any, Any]) -> list[TaskType]:
        tasks = await run_in_threadpool(_controller(info).list)
        return [TaskType.from_task(task) for task in tasks]

    @strawberry.field
    async def task(self, info: Info[Any, Any], id: strawberry.ID) -> TaskType | None:
        try:
            task = await run_in_threadpool(_controller(info).get, str(id))
        except TaskNotFound:
            return None
        return TaskType.from_task(task)

    @strawberry.field
    async def hello(self, info: Info[Any, Any]) -> str:
        outcome = await run_in_threadpool(_controller(info).execute, "hello")
        return outcome.output


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_task(self, info: Info[Any, Any], input: TaskInput) -> TaskType:
        try:
            task = await run_in_threadpool(_controller(info).create, input.prompt)
        except AFError as error:
            raise _graphql_error(error) from error
        return TaskType.from_task(task)

    @strawberry.mutation
    async def update_task(
        self,
        info: Info[Any, Any],
        id: strawberry.ID,
        input: TaskInput,
    ) -> TaskType:
        try:
            task = await run_in_threadpool(
                _controller(info).update,
                str(id),
                input.prompt,
            )
        except AFError as error:
            raise _graphql_error(error) from error
        return TaskType.from_task(task)

    @strawberry.mutation
    async def delete_task(self, info: Info[Any, Any], id: strawberry.ID) -> bool:
        try:
            await run_in_threadpool(_controller(info).delete, str(id))
        except AFError as error:
            raise _graphql_error(error) from error
        return True


schema = strawberry.Schema(query=Query, mutation=Mutation)


def create_graphql_router(controller: TaskController) -> GraphQLRouter[Any, Any]:
    async def context() -> dict[str, TaskController]:
        return {"controller": controller}

    return GraphQLRouter(schema, context_getter=context)
