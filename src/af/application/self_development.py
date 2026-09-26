"""One configured, exact-plan local development cycle with durable replay fencing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from af.application.engineering import LocalEngineeringService
from af.framework.context import ExecutionContext, derive_context
from af.framework.engineering import EngineeringCompletion
from af.framework.self_development import CycleRoute, SelfDevelopmentPlanner
from af.framework.self_development_execution import (
    DevelopmentCompletion,
    DevelopmentExecutor,
    DevelopmentLedger,
    DevelopmentPlanningInput,
    DevelopmentSource,
    PreparedDevelopment,
)
from af.framework.workflows import WorkflowError


def _authority_digest(context: ExecutionContext) -> str:
    principal = context.principal
    value = {
        "principal": None
        if principal is None
        else (
            principal.subject_id,
            principal.principal_type,
            sorted(principal.roles),
            sorted(principal.scopes),
            sorted(principal.claims),
        ),
        "tenant": context.tenant_id,
        "requested": context.requested_scope,
        "effective": context.effective_scope,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class SelfDevelopmentController:
    def __init__(
        self,
        *,
        source: DevelopmentSource | None = None,
        executor: DevelopmentExecutor | None = None,
        ledger: DevelopmentLedger | None = None,
        planner: SelfDevelopmentPlanner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source, self._executor, self._ledger = source, executor, ledger
        self._planner = planner or SelfDevelopmentPlanner()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _active(self, context: ExecutionContext) -> None:
        if context.cancellation.cancelled:
            raise ValueError("development_cancelled")
        if context.deadline is not None and context.deadline <= self._clock():
            raise ValueError("development_deadline_expired")

    def plan(
        self,
        inputs: DevelopmentPlanningInput | None = None,
        *,
        context: ExecutionContext,
    ) -> PreparedDevelopment:
        self._active(context)
        configured = inputs is None
        if inputs is None:
            if self._source is None:
                raise ValueError("self-development source is not configured; provide --snapshot")
            inputs = self._source.capture(context)
        plan = self._planner.plan(inputs.snapshot, inputs.policy, now=self._clock())
        prepared = PreparedDevelopment(
            inputs=inputs,
            plan=plan,
            plan_digest=plan.digest,
            authority_digest=_authority_digest(context),
        )
        if configured and self._ledger is not None:
            self._ledger.save(prepared)
        return prepared

    def execute(self, digest: str, context: ExecutionContext) -> DevelopmentCompletion:
        self._active(context)
        if self._source is None or self._executor is None or self._ledger is None:
            raise ValueError("self-development execution is not configured")
        prepared = self._ledger.load(digest)
        if prepared.authority_digest != _authority_digest(context):
            raise ValueError("self-development plan authority changed")
        fresh = self._source.capture(context)
        # Re-observe the repository and authority. Only observation time may change;
        # every fact that selected this exact work item must remain identical.
        original = prepared.inputs
        observed = replace(fresh.snapshot, captured_at=original.snapshot.captured_at)
        if (
            observed != original.snapshot
            or fresh.policy != original.policy
            or fresh.unit != original.unit
            or self._planner.plan(fresh.snapshot, fresh.policy, now=self._clock()).route
            is CycleRoute.STOP
        ):
            raise ValueError("self-development source or authority changed; prepare a fresh plan")
        if prepared.plan.route is CycleRoute.STOP:
            raise ValueError("self-development plan has no eligible work")
        completed = self._ledger.completed(digest)
        if completed is not None:
            return completed
        self._active(context)
        if not self._ledger.claim(digest):
            raise WorkflowError("self-development effect requires recovery or is already running")
        # An exception/crash retains the claim. An arbitrary partially completed
        # effect is never retried merely because its caller disconnected.
        result = self._executor.execute(prepared, context)
        self._active(context)
        completion = DevelopmentCompletion(plan_digest=digest, completion=result)
        self._ledger.finish(completion)
        return completion


class LocalEngineeringDevelopmentExecutor:
    """Execute only the exact finite host unit selected by the development planner."""

    def __init__(self, engineering: LocalEngineeringService) -> None:
        self._engineering = engineering

    def execute(
        self, prepared: PreparedDevelopment, context: ExecutionContext
    ) -> EngineeringCompletion:

        unit = prepared.inputs.unit
        selected = next(
            (
                item
                for item in prepared.inputs.snapshot.candidates
                if item.item_id == prepared.plan.selected_item_id
            ),
            None,
        )
        if (
            unit is None
            or selected is None
            or selected.item_id != unit.candidate_id
            or selected.revision != unit.digest
            or unit.expected_head != prepared.inputs.snapshot.head_sha
        ):
            raise ValueError("development unit does not bind the selected exact candidate")
        result: EngineeringCompletion = self._engineering.complete(
            unit.request,
            derive_context(
                context,
                deadline=min(
                    unit.expires_at,
                    context.deadline or unit.expires_at,
                ),
            ),
            expected_source_head=unit.expected_head,
        )
        return result
