"""Authorized admin panels call shared application queries and commands, never ORM APIs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict

from af.application.operations import OperationsController
from af.application.tasks import TaskController
from af.framework.admin import AdminAction, AdminActionResult, AdminResource, AdminRow
from af.framework.context import Principal
from af.framework.observability_products import ObservabilityDenied, SafeAttribute, identifier


class AdminService:
    def __init__(self, resources: tuple[AdminResource, ...]) -> None:
        if len(resources) > 32 or len({item.name for item in resources}) != len(resources):
            raise ValueError("invalid admin resource registry")
        for resource in resources:
            identifier(resource.name)
            if (
                not resource.read_scope
                or len(resource.actions) > 16
                or len({item.name for item in resource.actions}) != len(resource.actions)
            ):
                raise ValueError("invalid admin action registry")
            for action in resource.actions:
                identifier(action.name)
                if not action.required_scope or type(action.requires_confirmation) is not bool:
                    raise ValueError("invalid admin action authorization")
        self._resources = {item.name: item for item in resources}

    def resources(self, principal: Principal) -> tuple[str, ...]:
        return tuple(
            name
            for name, resource in self._resources.items()
            if resource.read_scope in principal.scopes
        )

    def query(self, principal: Principal, name: str, *, limit: int = 100) -> tuple[AdminRow, ...]:
        resource = self._resources.get(name)
        if resource is None or resource.read_scope not in principal.scopes:
            raise ObservabilityDenied()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid admin query limit")
        rows = resource.query(principal, limit)
        if (
            type(rows) is not tuple
            or len(rows) > limit
            or any(type(row) is not AdminRow for row in rows)
        ):
            raise ValueError("admin query returned an invalid bounded page")
        if len(json.dumps([asdict(row) for row in rows], allow_nan=False).encode()) > 65536:
            raise ValueError("admin page exceeds output bound")
        return rows

    def action(
        self,
        principal: Principal,
        resource_name: str,
        action_name: str,
        parameters: Mapping[str, SafeAttribute],
        *,
        confirmed: bool = False,
    ) -> AdminActionResult:
        resource = self._resources.get(resource_name)
        if resource is None:
            raise ObservabilityDenied()
        action = next((item for item in resource.actions if item.name == action_name), None)
        if (
            action is None
            or action.required_scope not in principal.scopes
            or (action.requires_confirmation and confirmed is not True)
        ):
            raise ObservabilityDenied()
        if (
            len(parameters) > 16
            or len(json.dumps(dict(parameters), allow_nan=False).encode()) > 8192
        ):
            raise ValueError("admin action parameters exceed bound")
        for key, value in parameters.items():
            identifier(key)
            if type(value) not in (str, int, float, bool) or (
                isinstance(value, str) and len(value) > 200
            ):
                raise ValueError("invalid admin action parameter")
        result = action.execute(principal, parameters)
        if type(result) is not AdminActionResult:
            raise ValueError("invalid admin command result")
        return result


def create_reference_admin(tasks: TaskController, operations: OperationsController) -> AdminService:
    """Compose metadata-only task and health panels from the existing use cases."""

    def task_rows(principal: Principal, limit: int) -> tuple[AdminRow, ...]:
        del principal
        return tuple(
            AdminRow(
                task.id,
                task.status,
                (
                    ("route", task.route),
                    ("tier", task.tier),
                    ("verified", task.verified),
                    ("reused", task.reused),
                ),
            )
            for task in tasks.list(limit=limit)
        )

    def delete_task(
        principal: Principal, parameters: Mapping[str, SafeAttribute]
    ) -> AdminActionResult:
        del principal
        if set(parameters) != {"task_id"} or not isinstance(parameters["task_id"], str):
            raise ValueError("task deletion requires exactly one task_id")
        task_id = parameters["task_id"]
        tasks.delete(task_id)
        return AdminActionResult("completed", task_id)

    def health_rows(principal: Principal, limit: int) -> tuple[AdminRow, ...]:
        del principal, limit
        runtime = operations.snapshot().runtime
        return (
            AdminRow(
                "application",
                runtime.lifecycle_state,
                (("liveness", runtime.liveness), ("readiness", runtime.readiness)),
            ),
        )

    return AdminService(
        (
            AdminResource(
                "tasks", "af:read", task_rows, (AdminAction("delete", "af:write", delete_task),)
            ),
            AdminResource("health", "af:read", health_rows),
        )
    )
