"""Offline executable fixtures for the bounded self-operation first slices.

These ports produce evidence only. They cannot mutate repositories, publish,
merge, or grant real operation authority.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from af.application.self_improvement import SelfImprovementService
from af.framework.evolution import EvolutionCandidate, EvolutionObservation, EvolutionSnapshot
from af.framework.self_improvement import (
    AdmittedStage,
    ImprovementSnapshot,
    SelfImprovementLedger,
    StageDispatch,
    StageEvaluation,
)
from af.framework.self_operation import (
    AuthorityRecord,
    SelfOperationSnapshot,
    SelfOperationStage,
    StageBudget,
    StageHandoff,
    StageReason,
    StageResult,
)
from af.infrastructure.database import Database
from af.infrastructure.self_improvement import SqlAlchemySelfImprovementLedger

_POLICY = "sha256:" + hashlib.sha256(b"af.offline.fixture.policy.v1").hexdigest()


class FixtureSelfImprovement:
    def __init__(
        self, ledger: SelfImprovementLedger, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._ledger = ledger
        self._clock = clock or (lambda: datetime.now(UTC))

    def capture(self, operation_id: str) -> ImprovementSnapshot:
        records = self._ledger.records(operation_id)
        completed = {
            record.dispatch.stage: record for record in records if record.evaluation is not None
        }
        source = SelfOperationSnapshot(
            "offline-fixture",
            "fixture/AgentFramework",
            "fixture",
            "0" * 40,
            self._clock() + timedelta(microseconds=len(records)),
            (_POLICY,),
            (AuthorityRecord("fixture-entry", "fixture-only", True),),
            (StageBudget("fixture-stages", "count", 100, len(completed)),),
        )
        handoffs = []
        predecessor = None
        for stage in SelfOperationStage:
            record = completed.get(stage)
            if record is not None and record.evaluation is not None:
                handoffs.append(record.dispatch.handoff)
                predecessor = record.evaluation.result.digest
            else:
                handoffs.append(
                    StageHandoff(
                        stage,
                        source.digest,
                        _POLICY,
                        _POLICY,
                        predecessor,
                        "fixture-entry" if predecessor is None else None,
                    )
                )
        return ImprovementSnapshot(source=source, handoffs=tuple(handoffs))

    def invoke(self, dispatch: StageDispatch, snapshot: ImprovementSnapshot) -> StageEvaluation:
        if dispatch.snapshot_digest != snapshot.digest:
            raise ValueError("fixture dispatch does not match snapshot")
        output = (
            "sha256:"
            + hashlib.sha256(
                f"fixture:{dispatch.stage}:{dispatch.handoff.digest}".encode()
            ).hexdigest()
        )
        result = StageResult(dispatch.stage, dispatch.handoff.digest, StageReason.SELECTED, output)
        return StageEvaluation(
            result=result,
            admission=AdmittedStage(
                independent=True,
                handoff=dispatch.handoff,
                result=result,
                reviewer="offline-contract-verifier",
                expires_at=self._clock() + timedelta(hours=1),
            ),
        )


def create_fixture_self_improvement_service(
    database: Database, *, authentication_key: bytes, clock: Callable[[], datetime] | None = None
) -> SelfImprovementService:
    ledger = SqlAlchemySelfImprovementLedger(database, authentication_key=authentication_key)
    fixture = FixtureSelfImprovement(ledger, clock=clock)
    return SelfImprovementService(fixture, fixture, ledger, clock=clock)


def reference_evolution_snapshot(*, now: datetime) -> EvolutionSnapshot:
    observations = tuple(
        EvolutionObservation(
            admitted=True,
            period_start=now - timedelta(days=5 - index),
            period_end=now - timedelta(days=4 - index),
            baseline=10,
            observed=15 + index,
            uncertainty=0.5,
            samples=100,
            held_out=index == 3,
            evidence_digest="sha256:"
            + hashlib.sha256(f"fixture-period-{index}".encode()).hexdigest(),
        )
        for index in range(4)
    )
    return EvolutionSnapshot(
        objective_id="fixture-reduce-local-routing-cost",
        metric="latency-ms",
        direction="minimize",
        captured_at=now,
        expires_at=now + timedelta(days=1),
        policy_digest=_POLICY,
        observations=observations,
        candidate=EvolutionCandidate(
            change="Fixture proposal: index installed procedure identities",
            artifact_digest=_POLICY,
            predicted_gain=(2, 4),
            rollback="Restore baseline linear scan",
            alternatives=("Retain baseline scanner",),
            guardrails=("No correctness regression",),
            counterfactual_digest=_POLICY,
            goodhart_checked=True,
        ),
    )
