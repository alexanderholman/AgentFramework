from __future__ import annotations

import subprocess
from pathlib import Path

from af.application.engineering_probe import InstalledEngineeringRouteProbe
from af.domain.classifiers import (
    CharacterCountClassifier,
    ClassifierChain,
    HelloClassifier,
    UnknownClassifier,
)
from af.domain.procedures import CharacterCountProcedure, HelloProcedure
from af.framework.context import ExecutionContext
from af.framework.engineering import EngineeringRequest
from af.framework.routing import ProcedureRegistry
from af.infrastructure.database import Database
from af.infrastructure.local_engineering import create_local_engineering_service


def probe():
    registry = ProcedureRegistry()
    registry.register(CharacterCountProcedure())
    registry.register(HelloProcedure())
    classifier = ClassifierChain(
        [HelloClassifier(), CharacterCountClassifier(), UnknownClassifier()]
    )
    return InstalledEngineeringRouteProbe(lambda: classifier, lambda: registry)


def service_factory(database, root, **kwargs):
    return create_local_engineering_service(database, root, route_probe=probe(), **kwargs)


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "fixture@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Fixture"], check=True)
    (root / "README.md").write_text("Independent test repository\n")
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "fixture"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "https://example.invalid/private.git"],
        check=True,
    )
    return root


def test_real_missing_component_is_engineered_verified_admitted_and_reused(tmp_path):
    root = repository(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    service = service_factory(database, root, authentication_key=b"k" * 32)
    request = EngineeringRequest(
        texts=("one\n\ntwo", "three"),
    )
    first = service.complete(request, ExecutionContext())
    assert first.output == 3
    assert first.engineered
    assert first.engineering_seconds > 0
    assert all(gate.passed for gate in first.admission.gates)
    assert Path(first.admission.artifact_path).is_file()
    import hashlib

    for gate in first.admission.gates:
        evidence = Path(first.admission.artifact_path).with_name(f"{gate.name}.json")
        assert hashlib.sha256(evidence.read_bytes()).hexdigest() == gate.evidence_digest
    repeated = service.complete(
        request.model_copy(update={"texts": ("alpha\n beta\n",)}), ExecutionContext()
    )
    assert repeated.output == 2
    assert not repeated.engineered
    assert repeated.engineering_seconds == 0
    assert repeated.admission.digest == first.admission.digest
    assert (
        subprocess.check_output(["git", "-C", str(root), "branch", "--show-current"]).strip()
        == b"main"
    )
    database.close()


def test_promotion_updates_classifier_and_invalidates_atomically(tmp_path):
    import pytest

    from af.application.engineering_routes import engineering_contribution
    from af.framework.context import use_context
    from af.framework.types import WorkRequest
    from af.framework.workflows import WorkflowError

    root = repository(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    service = service_factory(database, root, authentication_key=b"k" * 32)
    contribution = engineering_contribution(service)
    request = WorkRequest("count nonblank lines:one\n\ntwo")
    assert contribution.classifier.classify(request) is None
    first = service.complete(
        EngineeringRequest(
            texts=("one\n\ntwo",),
        ),
        ExecutionContext(),
    )
    assert {gate.name for gate in first.admission.gates} == {
        "test-first",
        "tests",
        "lint",
        "format",
        "typing",
        "coverage",
        "security",
        "compatibility",
        "governance",
        "independent-review",
    }
    classified = contribution.classifier.classify(request)
    assert classified is not None
    with use_context(ExecutionContext()):
        result = contribution.procedure.execute(classified)
    assert result.output == "2"
    assert result.evidence["engineering_seconds"] == 0
    assert contribution.verifier.verify(request, classified, contribution.procedure, result).passed
    assert contribution.classifier.classify(WorkRequest("count all lines:one\ntwo")) is None
    service.invalidate(first.admission.family, "failed later security audit", ExecutionContext())
    assert contribution.classifier.classify(request) is None
    with pytest.raises(WorkflowError, match="route_stale"):
        service.execute_admitted(("one",), first.admission.digest, ExecutionContext())
    database.close()


def test_denial_cancellation_and_deadline_create_no_engineering_artifact(tmp_path):
    from datetime import UTC, datetime, timedelta

    import pytest

    from af.framework.context import CancellationToken
    from af.framework.workflows import WorkflowError

    root = repository(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    service = service_factory(database, root, authentication_key=b"k" * 32)
    request = EngineeringRequest(
        texts=("one",),
    )
    token = CancellationToken()
    token.cancel()
    with pytest.raises(WorkflowError, match="cancelled"):
        service.complete(request, ExecutionContext(cancellation=token))
    with pytest.raises(WorkflowError, match="deadline_expired"):
        service.complete(
            request, ExecutionContext(deadline=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert not (root / ".git" / "af").exists()
    assert service.admitted(request.family) is None
    database.close()


def test_failed_independent_review_quarantines_candidate_and_no_workflow_executes(tmp_path):
    import pytest
    from sqlalchemy import text

    from af.framework.workflows import WorkflowError
    from af.infrastructure.engineering_catalogue import SqlAlchemyEngineeringCatalogue

    class Reviewer:
        identity = "independent-rejecting-reviewer"

        def review(self, source, gates):
            return False

    root = repository(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    service = service_factory(database, root, authentication_key=b"k" * 32, reviewer=Reviewer())
    request = EngineeringRequest(
        texts=("one",),
    )
    with pytest.raises(WorkflowError, match="review_failed"):
        service.complete(request, ExecutionContext())
    catalogue = SqlAlchemyEngineeringCatalogue(database, authentication_key=b"k" * 32)
    assert catalogue.status(request.family) == "rejected"
    assert catalogue.lookup(request.family) is None
    with database.engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM af_workflow_runs")).scalar() == 0
    with pytest.raises(WorkflowError, match="requires_recovery"):
        service.complete(request, ExecutionContext())
    assert list((root / ".git" / "af" / "worktrees").glob("*/_af_engineering/tests.json"))
    database.close()


def test_concurrent_first_requests_share_one_real_engineering_and_restart_reuses(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from af.infrastructure.local_engineering import FiniteTemplateReviewer

    entered, release = Event(), Event()

    class Reviewer:
        identity = "independent-concurrent-reviewer"
        calls = 0

        def review(self, source, gates):
            self.calls += 1
            entered.set()
            assert release.wait(10)
            return FiniteTemplateReviewer().review(source, gates)

    root = repository(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    reviewer = Reviewer()
    first = service_factory(database, root, authentication_key=b"k" * 32, reviewer=reviewer)
    second = service_factory(database, root, authentication_key=b"k" * 32, reviewer=reviewer)
    request = EngineeringRequest(
        texts=("one\n\ntwo",),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(first.complete, request, ExecutionContext())
        assert entered.wait(10)
        two = pool.submit(second.complete, request, ExecutionContext())
        release.set()
        a, b = one.result(15), two.result(15)
    assert a.output == b.output == 2
    assert a.engineered and not b.engineered
    assert reviewer.calls == 1
    assert a.admission.digest == b.admission.digest
    database.close()
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    restarted = service_factory(database, root, authentication_key=b"k" * 32)
    result = restarted.execute_admitted(
        ("new\nparameters\n",), a.admission.digest, ExecutionContext()
    )
    assert result.output == 2 and result.engineering_seconds == 0
    database.close()


def test_artifact_substitution_and_catalogue_rollback_cannot_restore_route(tmp_path):
    import pytest
    from sqlalchemy import update

    from af.framework.workflows import WorkflowError
    from af.infrastructure.engineering_catalogue import (
        EngineeringCatalogueRow,
        SqlAlchemyEngineeringCatalogue,
    )

    root = repository(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    service = service_factory(database, root, authentication_key=b"k" * 32)
    result = service.complete(
        EngineeringRequest(
            texts=("one",),
        ),
        ExecutionContext(),
    )
    with database.sessions() as session:
        row = session.get(EngineeringCatalogueRow, result.admission.family)
        previous = {"state": row.state, "signature": row.signature, "payload": row.payload}
    path = Path(result.admission.artifact_path)
    source = path.read_bytes()
    path.write_bytes(b'raise RuntimeError("substituted")\n')
    with pytest.raises(WorkflowError, match="artifact_changed"):
        service.admitted(result.admission.family)
    path.write_bytes(source)
    service.invalidate(result.admission.family, "rollback", ExecutionContext())
    with database.sessions.begin() as session:
        session.execute(update(EngineeringCatalogueRow).values(**previous))
    with pytest.raises(WorkflowError, match="stale_state"):
        service.admitted(result.admission.family)
    wrong_key = SqlAlchemyEngineeringCatalogue(database, authentication_key=b"z" * 32)
    with pytest.raises(WorkflowError):
        wrong_key.lookup(result.admission.family)
    database.close()


def test_cli_complete_status_reuse_rollback_and_bad_inputs(tmp_path):
    import json

    import pytest
    import typer
    from pydantic import ValidationError
    from typer.testing import CliRunner

    from af.application.engineering_controller import EngineeringController
    from af.application.engineering_routes import engineering_contribution
    from af.framework.context import use_context
    from af.framework.engineering import ComponentAdmission
    from af.framework.errors import InvalidSelectionProposal
    from af.framework.types import Classification, ProcedureResult, WorkRequest
    from af.framework.workflows import WorkflowError
    from af.interfaces.engineering import register_engineering_commands

    root = repository(tmp_path)
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    service = service_factory(database, root, authentication_key=b"k" * 32)
    cli = typer.Typer()
    register_engineering_commands(cli, lambda _: EngineeringController(service))
    runner = CliRunner()
    data = tmp_path / "request.json"
    data.write_text('{"texts":["one\\n\\ntwo","three"]}')
    with use_context(ExecutionContext()):
        assert runner.invoke(cli, ["engineering", "status"]).output.strip() == "null"
        first = runner.invoke(cli, ["engineer", "--input", str(data)])
        assert first.exit_code == 0, first.output
        payload = json.loads(first.output)
        assert payload["output"] == 3 and payload["engineered"]
        repeated = runner.invoke(cli, ["engineer", "--input", str(data)])
        assert repeated.exit_code == 0, repeated.output
        assert not json.loads(repeated.output)["engineered"]
        admitted = runner.invoke(cli, ["engineering", "status"])
        assert admitted.exit_code == 0
        admission = ComponentAdmission.model_validate_json(admitted.output)
        contribution = engineering_contribution(service)
        assert (
            contribution.classifier.classify(WorkRequest("count nonblank lines:" + "x" * 60_001))
            is None
        )
        with pytest.raises(InvalidSelectionProposal):
            contribution.procedure.validate(Classification("bad", {}, 1, "test"))
        with pytest.raises(InvalidSelectionProposal, match="too large"):
            contribution.procedure.validate(
                Classification(
                    "text.admitted_nonblank_line_total",
                    {"text": "x" * 60_001, "admission": admission.digest},
                    1,
                    "test",
                )
            )
        assert (
            contribution.verifier.verify(
                WorkRequest("hello"),
                Classification("hello", {}, 1, "test"),
                contribution.procedure,
                ProcedureResult("hello"),
            )
            is None
        )
        classification = contribution.classifier.classify(WorkRequest("count nonblank lines:one"))
        assert contribution.procedure.selection_is_grounded(
            WorkRequest("count nonblank lines:one"), classification
        )
        bad = admission.model_dump()
        bad["gates"] = ()
        with pytest.raises(ValidationError, match="requires all"):
            ComponentAdmission.model_validate(bad)
        bad = admission.model_dump()
        bad["reviewer"] = ""
        with pytest.raises(ValidationError, match="reviewer"):
            ComponentAdmission.model_validate(bad)
        assert runner.invoke(cli, ["engineering", "invalidate", "--reason", ""]).exit_code == 2
        invalidated = runner.invoke(cli, ["engineering", "invalidate", "--reason", "rollback-test"])
        assert invalidated.exit_code == 0
        with pytest.raises(InvalidSelectionProposal, match="route has changed"):
            contribution.procedure.validate(classification)
        data.write_bytes(b"x" * 262_145)
        assert runner.invoke(cli, ["engineer", "--input", str(data)]).exit_code == 2
        assert runner.invoke(cli, ["engineering", "invalidate", "--reason", "again"]).exit_code == 2
    absent = typer.Typer()
    register_engineering_commands(absent, lambda _: EngineeringController(None))
    assert runner.invoke(absent, ["engineering", "status"]).exit_code == 2
    with pytest.raises(WorkflowError, match="repository_not_configured"):
        EngineeringController(None).invalidate("reason", ExecutionContext())
    database.close()


def test_installed_application_engineering_direct_ask_restart_and_invalidation(tmp_path):
    import json

    from typer.testing import CliRunner

    from af.bootstrap import create_application
    from af.config import Settings
    from af.interfaces.console import create_cli

    root = repository(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'state.db'}", engineering_repository=str(root)
    )
    request = tmp_path / "request.json"
    request.write_text('{"texts":["one\\n\\ntwo"]}')
    runner = CliRunner()
    app = create_application(settings=settings)
    try:
        cli = create_cli(app)
        result = runner.invoke(cli, ["engineer", "--input", str(request)])
        assert result.exit_code == 0, result.output
        first = json.loads(result.output)
        assert first["engineered"] and first["output"] == 2
        result = runner.invoke(cli, ["ask", "count nonblank lines:one\n\ntwo", "--json"])
        assert result.exit_code == 0, result.output
        direct = json.loads(result.output)
        assert direct["output"] == "2" and direct["verified"]
        assert direct["route"] == "admitted-nonblank-line-total"
    finally:
        app.shutdown()
    app = create_application(settings=settings)
    try:
        cli = create_cli(app)
        result = runner.invoke(cli, ["ask", "count nonblank lines:new\nparameters\n", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["output"] == "2"
        result = runner.invoke(cli, ["engineer", "--input", str(request)])
        assert result.exit_code == 0, result.output
        assert not json.loads(result.output)["engineered"]
        result = runner.invoke(
            cli, ["engineering", "invalidate", "--reason", "rollback regression"]
        )
        assert result.exit_code == 0, result.output
        result = runner.invoke(cli, ["ask", "count nonblank lines:new\nparameters\n", "--json"])
        assert result.exit_code != 0
    finally:
        app.shutdown()
