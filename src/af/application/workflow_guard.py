"""Cooperative cancellation and fenced heartbeat for one running workflow node."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Event, Thread
from types import TracebackType

from af.framework.context import CancellationToken, ExecutionContext
from af.framework.workflows import (
    NodeState,
    WorkflowConflict,
    WorkflowError,
    WorkflowPolicy,
    WorkflowRepository,
    WorkflowResources,
    WorkflowRun,
)


class WorkflowGuard:
    def __init__(
        self,
        repository: WorkflowRepository,
        run: WorkflowRun,
        index: int,
        context: ExecutionContext,
        policy: WorkflowPolicy,
        resources: WorkflowResources | None,
        resource: str | None,
        lifetime: timedelta,
    ) -> None:
        self._repository = repository
        self._run = run
        self._index = index
        self._parent = context
        self.context = replace(context, cancellation=CancellationToken())
        self._policy = policy
        self._resources = resources
        self._resource = resource
        self._lifetime = lifetime
        self._stop = Event()
        self.failure: str | None = None
        self._thread = Thread(target=self._watch, name=f"af-workflow-{run.run_id}", daemon=True)

    def _check(self, *, renew: bool) -> None:
        if self._parent.cancellation.cancelled:
            raise WorkflowError("cancelled")
        latest = self._repository.get(self._run.run_id)
        record = latest.nodes[self._index]
        original = self._run.nodes[self._index]
        if latest.cancelled:
            raise WorkflowError("cancelled")
        if record.fence != original.fence or record.state != NodeState.RUNNING:
            raise WorkflowError("worker_fence_revoked")
        if record.lease_until is None or record.lease_until <= datetime.now(UTC):
            raise WorkflowError("claim_expired")
        self._policy.check(self.context, latest.definition.nodes[self._index], phase="heartbeat")
        if (
            self._resource is not None
            and self._resources is not None
            and not self._resources.valid(self._resource)
        ):
            raise WorkflowError("resource_lease_revoked")
        if renew:
            nodes = list(latest.nodes)
            nodes[self._index] = record.model_copy(
                update={"lease_until": datetime.now(UTC) + self._lifetime}
            )
            self._repository.save(
                latest.model_copy(update={"nodes": tuple(nodes)}), expected_revision=latest.revision
            )

    def _watch(self) -> None:
        renewal = self._lifetime.total_seconds() / 3
        interval = min(0.05, renewal)
        next_renewal = time.monotonic() + renewal
        while not self._stop.wait(interval):
            try:
                renew = time.monotonic() >= next_renewal
                self._check(renew=renew)
                if renew:
                    next_renewal = time.monotonic() + renewal
            except WorkflowConflict:
                # A cancellation or another fence winner is observed on the next poll.
                continue
            except Exception as exc:
                self.failure = (
                    str(exc) if isinstance(exc, WorkflowError) else "checkpoint_unavailable"
                )
                self.context.cancellation.cancel()
                return

    def __enter__(self) -> WorkflowGuard:
        self._check(renew=False)
        self._thread.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del kind, error, traceback
        self._stop.set()
        self._thread.join(timeout=1)
        if self._thread.is_alive():
            self.context.cancellation.cancel()
            raise WorkflowError("heartbeat_shutdown_failed")
