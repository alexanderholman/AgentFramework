from __future__ import annotations

import json
from datetime import UTC, datetime

import typer
from typer.testing import CliRunner

from af.application.self_stages import SelfStageController
from af.framework.self_operation import (
    AuthorityRecord,
    SelfOperationSnapshot,
    SelfOperationStage,
    StageBudget,
    StageHandoff,
)
from af.framework.self_stages import PriorityCandidate, StageEvidenceSnapshot
from af.interfaces.self_stages import register_lifecycle_stage_commands


def test_real_prioritisation_cli_uses_exact_supplied_evidence(tmp_path):
    digest = "sha256:" + "a" * 64
    now = datetime.now(UTC)
    source = SelfOperationSnapshot(
        "operator",
        "owner/repo",
        "main",
        "a" * 40,
        now,
        (digest,),
        (AuthorityRecord("entry", "self-prioritisation", True),),
        (StageBudget("stages", "count", 10, 0),),
    )
    evidence = StageEvidenceSnapshot(
        source=source,
        handoff=StageHandoff(
            SelfOperationStage.PRIORITISATION, source.digest, digest, digest, None, "entry"
        ),
        evaluated_at=now,
        policy_digest=digest,
        objective_id="speed",
        acceptance_clauses=("latency",),
        candidates=(
            PriorityCandidate(
                candidate_id="index",
                objective_id="speed",
                value=10,
                aligned=True,
                evidence_digest=digest,
            ),
        ),
    )
    path = tmp_path / "stage.json"
    path.write_text(evidence.model_dump_json())
    cli = typer.Typer()
    register_lifecycle_stage_commands(cli, lambda _: SelfStageController())
    result = CliRunner().invoke(cli, ["self-prioritise", "--snapshot", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["artifact"]["selected"] == "index"
    assert CliRunner().invoke(cli, ["self-contain", "--snapshot", str(path)]).exit_code == 2
    assert CliRunner().invoke(cli, ["self-prioritise"]).exit_code == 2
