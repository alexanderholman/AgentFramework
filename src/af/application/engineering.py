"""Compile one finite missing capability, admit atomically, execute and reuse."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from af.application.composition import ComponentContract, CompositionGap, CompositionPlanner
from af.application.workflows import WorkflowService
from af.framework.context import ExecutionContext
from af.framework.engineering import (
    ComponentAdmission,
    EngineeringCatalogue,
    EngineeringCompletion,
    EngineeringRequest,
    EngineeringRouteProbe,
    LocalComponentEngineer,
)
from af.framework.workflows import NodeState, WorkflowComponent, WorkflowError, WorkflowRepository


class LocalEngineeringService:
    def __init__(
        self,
        catalogue: EngineeringCatalogue,
        engineer: LocalComponentEngineer,
        workflows: WorkflowRepository,
        aggregate: WorkflowComponent,
        *,
        route_probe: EngineeringRouteProbe | None = None,
        wait_seconds: float = 60,
    ) -> None:
        if not 0 < wait_seconds <= 1200:
            raise ValueError("engineering wait must be bounded")
        self._catalogue = catalogue
        self._engineer = engineer
        self._workflows = workflows
        self._aggregate = aggregate
        self._wait_seconds = wait_seconds
        self._route_probe = route_probe

    def admitted(self, family: str) -> ComponentAdmission | None:
        admission = self._catalogue.lookup(family)
        if admission is not None:
            self._engineer.component(admission)
        return admission

    @staticmethod
    def _active(context: ExecutionContext) -> None:
        if context.cancellation.cancelled:
            raise WorkflowError("engineering_cancelled")
        if context.deadline is not None and context.deadline <= datetime.now(UTC):
            raise WorkflowError("engineering_deadline_expired")

    def complete(
        self, request: EngineeringRequest, context: ExecutionContext
    ) -> EngineeringCompletion:
        self._active(context)
        admission = self._catalogue.lookup(request.family)
        engineered = False
        engineering_seconds = 0.0
        if admission is None:
            if self._route_probe is None:
                raise WorkflowError("engineering_route_probe_unavailable")
            evidence = self._route_probe.probe(request, context)
            self._active(context)
            if evidence.status != "no-route-exhausted":
                raise WorkflowError(f"engineering_requires_exhaustive_no_route:{evidence.status}")
            gap = CompositionPlanner(
                (ComponentContract(self._aggregate, "number.vector.v1", "number.scalar.v1"),)
            ).compose(request.family, "text.line-batch.v1", "number.scalar.v1")
            if not isinstance(gap, CompositionGap):
                raise WorkflowError("engineering_gap_not_proven")
            owner = uuid4().hex
            if self._catalogue.claim(request.family, owner):
                started = time.monotonic()
                try:
                    admission = self._engineer.engineer(
                        request.family, context, route_history=evidence.history
                    )
                    self._active(context)
                    self._catalogue.admit(admission, owner)
                    engineered = True
                    engineering_seconds = time.monotonic() - started
                except BaseException:
                    self._catalogue.reject(request.family, owner, "engineering_gate_failed")
                    raise
            else:
                deadline = time.monotonic() + self._wait_seconds
                while admission is None:
                    self._active(context)
                    admission = self._catalogue.lookup(request.family)
                    if admission is not None:
                        break
                    if self._catalogue.status(request.family) not in {"engineering", "admitted"}:
                        raise WorkflowError("engineering_requires_recovery")
                    if time.monotonic() >= deadline:
                        raise WorkflowError("engineering_in_progress")
                    time.sleep(0.02)
                    admission = self._catalogue.lookup(request.family)
        return self._execute(admission, request.texts, context, engineered, engineering_seconds)

    def execute_admitted(
        self, texts: tuple[str, ...], digest: str, context: ExecutionContext
    ) -> EngineeringCompletion:
        from af.framework.engineering import FAMILY

        request = EngineeringRequest(texts=texts)
        admission = self._catalogue.lookup(FAMILY)
        if admission is None or admission.digest != digest:
            raise WorkflowError("admitted_engineering_route_stale")
        return self._execute(admission, request.texts, context, False, 0.0)

    def invalidate(self, family: str, reason: str, context: ExecutionContext) -> None:
        self._active(context)
        if not reason or len(reason) > 512:
            raise ValueError("invalidation requires a bounded reason")
        self._catalogue.invalidate(family, reason)

    def _execute(
        self,
        admission: ComponentAdmission,
        texts: tuple[str, ...],
        context: ExecutionContext,
        engineered: bool,
        engineering_seconds: float,
    ) -> EngineeringCompletion:
        self._active(context)
        component = self._engineer.component(admission)
        workflow = WorkflowService(self._workflows, (component, self._aggregate))
        payload = json.dumps({"texts": texts}, ensure_ascii=False).encode()
        started = time.monotonic()
        run = workflow.submit(admission.definition, payload, context)
        completed = workflow.resume(run.run_id, context)
        if completed.nodes[-1].state != NodeState.COMPLETED:
            raise WorkflowError("engineered_composite_verification_failed")
        result = json.loads(completed.nodes[-1].output or b"{}")
        output = result.get("total")
        if type(output) is not int or output < 0:
            raise WorkflowError("engineered_composite_output_invalid")
        return EngineeringCompletion(
            output=output,
            run_id=run.run_id,
            engineered=engineered,
            engineering_seconds=engineering_seconds,
            execution_seconds=time.monotonic() - started,
            admission=admission,
        )
