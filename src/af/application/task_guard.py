"""Cooperative Task cancellation and a commit fence shared by every transport."""

from collections.abc import Callable
from datetime import UTC, datetime

from af.framework.context import ExecutionContext
from af.framework.errors import AFError


class TaskOperationCancelled(AFError):
    def __init__(self) -> None:
        super().__init__("task operation cancelled or deadline exceeded")


def check_task_context(context: ExecutionContext | None) -> None:
    if context is not None and (
        context.cancellation.cancelled
        or (context.deadline is not None and datetime.now(UTC) >= context.deadline)
    ):
        raise TaskOperationCancelled()


def commit_task_write[T](context: ExecutionContext | None, action: Callable[[], T]) -> T:
    """Cancellation fences commits; effects already committed are not rolled back.

    Capture the context before entering user code. Ambient request authority can
    have been revoked by the time a cancelled worker thread returns.
    """
    if context is None:
        return action()
    result: list[T] = []

    def commit() -> None:
        # This runs under the cancellation token lock: do not reacquire that lock.
        if context.deadline is not None and datetime.now(UTC) >= context.deadline:
            raise TaskOperationCancelled()
        result.append(action())

    if not context.cancellation.run_if_active(commit):
        raise TaskOperationCancelled()
    return result[0]
