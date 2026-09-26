"""Bounded fixture-stage coordinator; no implicit repository/publication authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from af.framework.context import CancellationToken
from af.framework.self_improvement import (
    ImprovementBudget,
    ImprovementStop,
    SelfImprovementLedger,
    SelfImprovementSnapshotPort,
    SelfImprovementStagePort,
    next_improvement_stage,
)


class SelfImprovementService:
    def __init__(
        self,
        snapshots: SelfImprovementSnapshotPort,
        stages: SelfImprovementStagePort,
        ledger: SelfImprovementLedger,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self._snapshots = snapshots
        self._stages = stages
        self._ledger = ledger
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        operation_id: str,
        budget: ImprovementBudget,
        *,
        cancellation: CancellationToken | None = None,
    ) -> ImprovementStop:
        token = cancellation or CancellationToken()
        for _ in range(budget.maximum_stages + 1):
            snapshot = self._snapshots.capture(operation_id)
            records = self._ledger.records(operation_id)
            next_step = next_improvement_stage(
                operation_id,
                snapshot,
                records,
                budget,
                now=self._clock(),
                cancelled=token.cancelled,
            )
            if isinstance(next_step, ImprovementStop):
                return next_step
            if not self._ledger.reserve(next_step):
                return ImprovementStop(
                    operation_id=operation_id,
                    reason="concurrent_stage_claim",
                    stage=next_step.stage,
                )
            # A crash or exception after reservation deliberately leaves an uncertain
            # claim. No generic wrapper can prove an arbitrary external effect did not run.
            result = self._stages.invoke(next_step, snapshot)
            self._ledger.complete(next_step, result)
        return ImprovementStop(operation_id=operation_id, reason="stage_budget_exhausted")
