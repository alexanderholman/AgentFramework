"""Durable sequential DAG coordinator shared by transport and queue adapters.

Execution is intentionally serialized per run. Fenced checkpoint commits reject
late workers. Uncertain non-replay-safe effects require explicit human recovery;
no subprocess or arbitrary executable is silently retried.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from af.application.workflow_guard import WorkflowGuard
from af.framework.context import ExecutionContext, derive_context
from af.framework.workflows import (
    NodeRecord,
    NodeState,
    WorkflowComponent,
    WorkflowConflict,
    WorkflowDefinition,
    WorkflowError,
    WorkflowNode,
    WorkflowOutput,
    WorkflowPolicy,
    WorkflowProgress,
    WorkflowRepository,
    WorkflowResources,
    WorkflowRun,
)

_ACTIVE = {NodeState.ADMITTED, NodeState.RUNNING, NodeState.CHECKPOINTING}
_READY = {NodeState.QUEUED, NodeState.BLOCKED, NodeState.RETRYING}
_TERMINAL = {
    NodeState.COMPLETED,
    NodeState.UNSUPPORTED,
    NodeState.FAILED,
    NodeState.CANCELLED,
    NodeState.INCONCLUSIVE,
    NodeState.NEEDS_HUMAN,
}


def _authority(context: ExecutionContext) -> str:
    principal = context.principal
    material = {
        "principal": None
        if principal is None
        else {
            "subject": principal.subject_id,
            "type": principal.principal_type,
            "roles": sorted(principal.roles),
            "scopes": sorted(principal.scopes),
            "claims": sorted(principal.claims),
        },
        "tenant": context.tenant_id,
        "requested": context.requested_scope,
        "effective": context.effective_scope,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


class LocalWorkflowPolicy:
    """Local execution policy; richer authorization/consent checks are injectable."""

    version = "local-workflow-v1"

    def check(self, context: ExecutionContext, node: WorkflowNode, *, phase: str) -> None:
        del node, phase
        if context.cancellation.cancelled:
            raise WorkflowError("cancelled")
        if context.deadline is not None and context.deadline <= datetime.now(UTC):
            raise WorkflowError("deadline_expired")


class WorkflowService:
    def __init__(
        self,
        repository: WorkflowRepository,
        components: tuple[WorkflowComponent, ...],
        *,
        policy: WorkflowPolicy | None = None,
        resources: WorkflowResources | None = None,
        claim_lifetime: timedelta = timedelta(minutes=15),
    ) -> None:
        if claim_lifetime <= timedelta(0):
            raise ValueError("claim lifetime must be positive")
        if len({component.component_id for component in components}) != len(components):
            raise ValueError("component identities must be unique")
        self._repository = repository
        self.components = components
        self._components = {component.component_id: component for component in components}
        self._policy = policy or LocalWorkflowPolicy()
        self._resources = resources
        self._claim_lifetime = claim_lifetime

    def submit(
        self, definition: WorkflowDefinition, data: bytes, context: ExecutionContext
    ) -> WorkflowRun:
        if definition.compatibility_version != 1:
            raise WorkflowError("workflow compatibility version is unsupported")
        for node in definition.nodes:
            self._component(node)
            self._policy.check(context, node, phase="submit")
        now = datetime.now(UTC)
        return self._repository.create(
            WorkflowRun(
                run_id=str(uuid4()),
                definition=definition,
                input_data=data,
                input_digest=hashlib.sha256(data).hexdigest(),
                authority_digest=_authority(context),
                policy_version=self._policy.version,
                created_at=now,
                updated_at=now,
                deadline=context.deadline,
                nodes=tuple(
                    NodeRecord(
                        node_id=node.node_id,
                        state=NodeState.BLOCKED if node.dependencies else NodeState.QUEUED,
                    )
                    for node in definition.nodes
                ),
            )
        )

    def _component(self, node: WorkflowNode) -> WorkflowComponent:
        component = self._components.get(node.component_id)
        if component is None or component.digest != node.component_digest:
            raise WorkflowError("component unavailable or incompatible; restore pinned version")
        return component

    def get(self, run_id: str) -> WorkflowRun:
        return self._repository.get(run_id)

    def inspect(self, run_id: str, context: ExecutionContext) -> WorkflowRun:
        """Read an authenticated checkpoint only under its original authority."""
        run = self._repository.get(run_id)
        self._validate(run, context)
        for node in run.definition.nodes:
            self._policy.check(self._child(run, context, node), node, phase="inspection")
        return run

    def _validate(self, run: WorkflowRun, context: ExecutionContext) -> None:
        if run.authority_digest != _authority(context):
            raise WorkflowError("checkpoint authority changed; explicit recovery required")
        if run.policy_version != self._policy.version:
            raise WorkflowError("checkpoint policy version is incompatible")
        if run.definition.compatibility_version != 1:
            raise WorkflowError("checkpoint compatibility version is unsupported")
        if hashlib.sha256(run.input_data).hexdigest() != run.input_digest:
            raise WorkflowError("checkpoint input digest mismatch")
        if tuple(node.node_id for node in run.nodes) != tuple(
            node.node_id for node in run.definition.nodes
        ):
            raise WorkflowError("checkpoint nodes do not match definition")
        for node in run.definition.nodes:
            self._component(node)
        for record in run.nodes:
            if record.state == NodeState.COMPLETED and (
                not record.verified
                or record.output is None
                or record.verifier != "component"
                or hashlib.sha256(record.output).hexdigest() != record.output_digest
            ):
                raise WorkflowError("checkpoint completion lacks valid verifier evidence")

    def _record(self, run: WorkflowRun, index: int, record: NodeRecord) -> WorkflowRun:
        nodes = list(run.nodes)
        nodes[index] = record
        return self._repository.save(
            run.model_copy(update={"nodes": tuple(nodes)}), expected_revision=run.revision
        )

    def _child(
        self, run: WorkflowRun, context: ExecutionContext, node: WorkflowNode
    ) -> ExecutionContext:
        deadlines = [
            deadline for deadline in (context.deadline, run.deadline) if deadline is not None
        ]
        return derive_context(
            context,
            deadline=min(deadlines) if deadlines else None,
            causation_id=f"{run.run_id}:{node.node_id}",
        )

    def resume(
        self, run_id: str, context: ExecutionContext, *, max_nodes: int = 128
    ) -> WorkflowRun:
        if not 1 <= max_nodes <= 128:
            raise ValueError("max_nodes must be between 1 and 128")
        run = self._repository.get(run_id)
        self._validate(run, context)
        if run.cancelled:
            return run
        if context.cancellation.cancelled:
            return self.cancel(run_id, context)
        for _ in range(max_nodes):
            run = self._recover(run)
            # A valid active fence exclusively owns the run until completion/expiry.
            if any(record.state in _ACTIVE for record in run.nodes):
                return run
            index = self._ready(run)
            if index is None:
                return run
            try:
                run = self._execute(run, index, context)
            except WorkflowConflict:
                return self._repository.get(run_id)
            if run.cancelled or run.nodes[index].reason == "resource_contention":
                return run
        return run

    def _recover(self, run: WorkflowRun) -> WorkflowRun:
        now = datetime.now(UTC)
        for index, record in enumerate(run.nodes):
            if record.state not in _ACTIVE:
                continue
            if record.lease_until is not None and record.lease_until > now:
                continue
            component = self._component(run.definition.nodes[index])
            if component.replay_safe:
                record = record.model_copy(
                    update={
                        "state": NodeState.RETRYING,
                        "fence": None,
                        "lease_until": None,
                        "reason": "worker_lost_replay_safe",
                    }
                )
            else:
                record = record.model_copy(
                    update={
                        "state": NodeState.NEEDS_HUMAN,
                        "fence": None,
                        "lease_until": None,
                        "reason": "unproven_effect_after_worker_loss",
                    }
                )
            run = self._record(run, index, record)
        return run

    @staticmethod
    def _ready(run: WorkflowRun) -> int | None:
        records = {record.node_id: record for record in run.nodes}
        for index, (node, record) in enumerate(zip(run.definition.nodes, run.nodes, strict=True)):
            if record.state not in _READY:
                continue
            if all(
                records[gate.node_id].state == NodeState.COMPLETED
                and records[gate.node_id].verified
                and records[gate.node_id].verifier == gate.verifier
                and len(records[gate.node_id].output or b"") >= gate.minimum_bytes
                for gate in node.dependencies
            ):
                return index
        return None

    def _execute(self, run: WorkflowRun, index: int, context: ExecutionContext) -> WorkflowRun:
        node = run.definition.nodes[index]
        record = run.nodes[index]
        component = self._component(node)
        child = self._child(run, context, node)
        effect_key = f"{run.run_id}:{node.node_id}:{node.component_digest}"
        resource: str | None = None
        try:
            self._policy.check(child, node, phase="admission")
            if self._resources is not None:
                resource = self._resources.acquire(child, node, effect_key=effect_key)
                if resource is None:
                    return self._record(
                        run,
                        index,
                        record.model_copy(
                            update={"state": NodeState.BLOCKED, "reason": "resource_contention"}
                        ),
                    )
            record = record.model_copy(
                update={
                    "state": NodeState.ADMITTED,
                    "fence": str(uuid4()),
                    "lease_until": datetime.now(UTC) + self._claim_lifetime,
                    "attempt": record.attempt + 1,
                    "reason": None,
                }
            )
            run = self._record(run, index, record)
            self._policy.check(child, node, phase="effect")
            if (
                resource is not None
                and self._resources is not None
                and not self._resources.valid(resource)
            ):
                raise WorkflowError("resource_lease_revoked")
            record = record.model_copy(update={"state": NodeState.RUNNING})
            run = self._record(run, index, record)
            predecessors = {
                gate.node_id: next(
                    item.output or b"" for item in run.nodes if item.node_id == gate.node_id
                )
                for gate in node.dependencies
            }
            started = time.monotonic()
            with WorkflowGuard(
                self._repository,
                run,
                index,
                child,
                self._policy,
                self._resources,
                resource,
                self._claim_lifetime,
            ) as guard:
                child = guard.context
                output = component.handler.execute(
                    run.input_data, predecessors, child, effect_key=effect_key
                )
                if (
                    not isinstance(output, WorkflowOutput)
                    or type(output.data) is not bytes
                    or len(output.data) > 65_536
                ):
                    raise WorkflowError("invalid_component_output")
                verified = component.handler.verify(run.input_data, predecessors, output)
            latest = self._repository.get(run.run_id)
            if (
                latest.cancelled
                or latest.nodes[index].fence != record.fence
                or latest.nodes[index].state != NodeState.RUNNING
            ):
                raise WorkflowConflict("completion fence is stale")
            run = latest
            record = run.nodes[index]
            if guard.failure is not None:
                raise WorkflowError(guard.failure)
            duration = time.monotonic() - started
            if verified is not True:
                return self._record(
                    run,
                    index,
                    record.model_copy(
                        update={
                            "state": NodeState.INCONCLUSIVE,
                            "reason": "verification_failed",
                            "fence": None,
                            "lease_until": None,
                            "elapsed_seconds": duration,
                        }
                    ),
                )
            self._policy.check(child, node, phase="acceptance")
            if (
                resource is not None
                and self._resources is not None
                and not self._resources.valid(resource)
            ):
                raise WorkflowError("resource_lease_revoked")
            # Recheck persisted fencing/cancellation after the potentially long handler.
            latest = self._repository.get(run.run_id)
            if latest.revision != run.revision or latest.cancelled:
                raise WorkflowConflict("completion fence is stale")
            if record.lease_until is None or record.lease_until <= datetime.now(UTC):
                raise WorkflowError("claim_expired_before_acceptance")

            def persist() -> None:
                nonlocal run, record
                record = record.model_copy(update={"state": NodeState.CHECKPOINTING})
                run = self._record(run, index, record)
                record = record.model_copy(
                    update={
                        "state": NodeState.COMPLETED,
                        "output": output.data,
                        "output_digest": hashlib.sha256(output.data).hexdigest(),
                        "verified": True,
                        "verifier": "component",
                        "elapsed_seconds": duration,
                        "fence": None,
                        "lease_until": None,
                    }
                )
                run = self._record(run, index, record)

            def child_commit() -> None:
                if not child.cancellation.commit_completion(persist):
                    raise WorkflowError("cancelled")

            if not context.cancellation.run_if_active(child_commit):
                return self.cancel(run.run_id, context)
            return run
        except WorkflowConflict:
            raise
        except Exception as exc:
            if context.cancellation.cancelled:
                return self.cancel(run.run_id, context)
            reason = str(exc) if isinstance(exc, WorkflowError) else "component_failed"
            # Do not persist raw handler exception text, which can contain governed data.
            return self._record(
                run,
                index,
                record.model_copy(
                    update={
                        "state": NodeState.NEEDS_HUMAN
                        if record.state in _ACTIVE
                        else NodeState.FAILED,
                        "reason": reason[:128],
                        "fence": None,
                        "lease_until": None,
                    }
                ),
            )
        finally:
            if resource is not None and self._resources is not None:
                self._resources.release(resource)

    def cancel(self, run_id: str, context: ExecutionContext) -> WorkflowRun:
        for _ in range(8):
            run = self._repository.get(run_id)
            self._validate(run, context)
            if run.cancelled:
                return run
            nodes = tuple(
                record
                if record.state in _TERMINAL
                else record.model_copy(
                    update={
                        "state": NodeState.CANCELLED,
                        "reason": "cancelled_checkpoint_retained",
                        "fence": None,
                        "lease_until": None,
                    }
                )
                for record in run.nodes
            )
            try:
                return self._repository.save(
                    run.model_copy(
                        update={"cancelled": True, "nodes": nodes, "checkpoint_policy": "retained"}
                    ),
                    expected_revision=run.revision,
                )
            except WorkflowConflict:
                continue
        raise WorkflowConflict("cancellation contention; retry with same run ID")

    def invalidate(self, run_id: str, node_id: str, context: ExecutionContext) -> WorkflowRun:
        run = self._repository.get(run_id)
        self._validate(run, context)
        invalid = {node_id}
        if node_id not in {node.node_id for node in run.nodes}:
            raise WorkflowError("node not found")
        while True:
            dependents = {
                node.node_id
                for node in run.definition.nodes
                if any(gate.node_id in invalid for gate in node.dependencies)
            }
            if dependents <= invalid:
                break
            invalid.update(dependents)
        nodes = tuple(
            record.model_copy(
                update={
                    "state": NodeState.INCONCLUSIVE,
                    "verified": False,
                    "reason": "evidence_invalidated",
                    "fence": None,
                    "lease_until": None,
                }
            )
            if record.node_id in invalid
            else record
            for record in run.nodes
        )
        return self._repository.save(
            run.model_copy(update={"nodes": nodes}), expected_revision=run.revision
        )

    def progress(
        self, run_id: str, *, stale_after: timedelta = timedelta(minutes=1)
    ) -> WorkflowProgress:
        run = self._repository.get(run_id)
        completed = [record for record in run.nodes if record.state == NodeState.COMPLETED]
        observed = [
            record.elapsed_seconds for record in completed if record.elapsed_seconds is not None
        ]
        remaining = len(run.nodes) - len(completed)
        blocked = any(
            (record.state in _TERMINAL and record.state != NodeState.COMPLETED)
            or record.reason == "resource_contention"
            for record in run.nodes
        )
        eta = None
        if not remaining:
            eta = (0.0, 0.0)
        elif observed and not blocked:
            eta = (min(observed) * remaining * 0.5, max(observed) * remaining * 2)
        return WorkflowProgress(
            fraction=len(completed) / len(run.nodes),
            states=tuple((record.node_id, record.state) for record in run.nodes),
            eta_seconds=eta,
            confidence=min(0.8, len(observed) / len(run.nodes)),
            assumptions=("remaining nodes have comparable cost; resources remain available",)
            if eta is not None and remaining
            else (),
            observed_nodes=len(observed),
            updated_at=run.updated_at,
            stale=datetime.now(UTC) - run.updated_at > stale_after,
        )
