from __future__ import annotations

from datetime import UTC, datetime, timedelta

from af.application.self_improvement import SelfImprovementService
from af.framework.self_improvement import (
    AdmittedStage,
    ImprovementBudget,
    ImprovementSnapshot,
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

D = "sha256:" + "a" * 64
NOW = datetime(2026, 9, 26, tzinfo=UTC)


class Lifecycle:
    def __init__(self):
        self.calls = []
        self.handoffs = {}
        self.sequence = 0

    def capture(self, operation_id):
        self.sequence += 1
        source = SelfOperationSnapshot(
            "fixture",
            "owner/repo",
            "main",
            "a" * 40,
            NOW + timedelta(seconds=self.sequence),
            (D,),
            (AuthorityRecord("local", "self-improve", True),),
            (StageBudget("stages", "count", 100, 0),),
        )
        handoffs = tuple(
            self.handoffs.get(stage) or StageHandoff(stage, source.digest, D, D, None, "local")
            for stage in SelfOperationStage
        )
        return ImprovementSnapshot(source=source, handoffs=handoffs)

    def invoke(self, dispatch, snapshot):
        self.calls.append(dispatch.stage)
        self.handoffs[dispatch.stage] = dispatch.handoff
        result = StageResult(dispatch.stage, dispatch.handoff.digest, StageReason.SELECTED, D)
        return StageEvaluation(
            result=result,
            admission=AdmittedStage(
                independent=True,
                handoff=dispatch.handoff,
                result=result,
                reviewer="fixture-independent-review",
                expires_at=NOW + timedelta(hours=1),
            ),
        )


def test_full_fixture_lifecycle_and_restart_skip_exact_admitted_stages(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'self-improve.db'}")
    ledger = SqlAlchemySelfImprovementLedger(database, authentication_key=b"k" * 32)
    fixture = Lifecycle()
    service = SelfImprovementService(fixture, fixture, ledger, clock=lambda: NOW)
    budget = ImprovementBudget(deadline=NOW + timedelta(hours=1))
    result = service.run("operation-1", budget)
    assert result.reason == "complete"
    assert fixture.calls == list(SelfOperationStage)
    assert len(ledger.records("operation-1")) == 20
    assert service.run("operation-1", budget).reason == "complete"
    assert len(fixture.calls) == 10
    database.close()


def test_interruption_after_reservation_never_repeats_uncertain_stage(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'self-improve.db'}")
    ledger = SqlAlchemySelfImprovementLedger(database, authentication_key=b"k" * 32)
    fixture = Lifecycle()
    from af.framework.self_improvement import next_improvement_stage

    budget = ImprovementBudget(deadline=NOW + timedelta(hours=1))
    dispatch = next_improvement_stage(
        "operation-1", fixture.capture("operation-1"), (), budget, now=NOW
    )
    assert ledger.reserve(dispatch)
    restarted = SelfImprovementService(fixture, fixture, ledger, clock=lambda: NOW)
    assert restarted.run("operation-1", budget).reason == "interrupted_effect_requires_recovery"
    assert not fixture.calls
    database.close()
