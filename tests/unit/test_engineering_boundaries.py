"""Boundary failures that must never turn into implicit engineering authority."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from af.application.engineering import LocalEngineeringService
from af.application.engineering_probe import InstalledEngineeringRouteProbe
from af.domain.classifiers import HelloClassifier, UnknownClassifier
from af.domain.procedures import HelloProcedure
from af.framework.context import CancellationToken, ExecutionContext
from af.framework.engineering import EngineeringRequest
from af.framework.programs import ProgramResult, ProgramState
from af.framework.routing import ProcedureRegistry
from af.framework.types import Classification
from af.framework.workflows import WorkflowError
from af.infrastructure.database import Database
from af.infrastructure.engineering_catalogue import (
    EngineeringCatalogueRow,
    SqlAlchemyEngineeringCatalogue,
)
from af.infrastructure.local_engineering import (
    FiniteTemplateReviewer,
    GitTemplateEngineer,
    _execute,
    create_local_engineering_service,
)
from af.infrastructure.programs import AsyncSubprocessExecutor


def test_input_and_unavailable_route_proof_fail_closed(tmp_path):
    with pytest.raises(ValidationError, match="exceeds its bound"):
        EngineeringRequest(texts=("x" * 60_001,))
    with pytest.raises(ValidationError, match="encoded data exceeds"):
        EngineeringRequest(texts=("\x00" * 11_000,))
    with pytest.raises(ValidationError):
        EngineeringRequest.model_validate({"texts": ["one"], "lower_status": "no-route-exhausted"})
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    service = create_local_engineering_service(database, tmp_path, authentication_key=b"k" * 32)
    with pytest.raises(WorkflowError, match="probe_unavailable"):
        service.complete(EngineeringRequest(texts=("one",)), ExecutionContext())
    registry = ProcedureRegistry()
    probe = InstalledEngineeringRouteProbe(
        lambda: UnknownClassifier(), lambda: registry, selector_configured=True
    )
    service = create_local_engineering_service(
        database, tmp_path, authentication_key=b"k" * 32, route_probe=probe
    )
    with pytest.raises(WorkflowError, match="exhaustive_no_route:unavailable"):
        service.complete(EngineeringRequest(texts=("one",)), ExecutionContext())
    with pytest.raises(ValueError, match="bounded"):
        LocalEngineeringService(None, None, None, None, wait_seconds=0)
    database.close()


def test_route_probe_distinguishes_existing_missing_and_unknown_without_execution():
    registry = ProcedureRegistry()
    registry.register(HelloProcedure())

    class Existing:
        def classify(self, request):
            return Classification("system.hello", {}, 1.0, "test-classifier")

    request = EngineeringRequest(texts=("one",))
    probe = InstalledEngineeringRouteProbe(lambda: Existing(), lambda: registry)
    assert probe.probe(request, ExecutionContext()).status == "available"
    empty = ProcedureRegistry()
    probe = InstalledEngineeringRouteProbe(lambda: Existing(), lambda: empty)
    assert probe.probe(request, ExecutionContext()).status == "unavailable"
    probe = InstalledEngineeringRouteProbe(lambda: HelloClassifier(), lambda: empty)
    assert probe.probe(request, ExecutionContext()).status == "no-route-exhausted"
    cancellation = CancellationToken()
    cancellation.cancel()
    assert probe.probe(request, ExecutionContext(cancellation=cancellation)).status == "cancelled"


def test_catalogue_authentication_claim_fencing_and_history_fail_closed(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    with pytest.raises(ValueError, match="32 bytes"):
        SqlAlchemyEngineeringCatalogue(database, authentication_key=b"short")
    catalogue = SqlAlchemyEngineeringCatalogue(database, authentication_key=b"k" * 32)
    assert catalogue.status("family") is None
    assert catalogue.claim("family", "one")
    assert not catalogue.claim("family", "two")
    catalogue.reject("family", "wrong-owner", "rejected")
    assert catalogue.status("family") == "engineering"
    with pytest.raises(WorkflowError, match="fence_rejected"):
        catalogue.invalidate("family", "not admitted")
    wrong = SqlAlchemyEngineeringCatalogue(database, authentication_key=b"z" * 32)
    with pytest.raises(WorkflowError, match="authentication_failed"):
        wrong.status("family")
    with database.sessions.begin() as session:
        row = session.get(EngineeringCatalogueRow, "family")
        session.delete(row)
    with pytest.raises(WorkflowError, match="history_missing"):
        catalogue.status("family")
    database.close()


def test_cancellation_terminates_actual_program_and_time_boundaries(tmp_path):
    import sys
    import time

    token = CancellationToken()
    signal = tmp_path / "started"
    argv = (
        sys.executable,
        "-I",
        "-c",
        'import pathlib,time; pathlib.Path(__import__("sys").argv[1]).touch(); time.sleep(30)',
        str(signal),
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            _execute,
            AsyncSubprocessExecutor(),
            argv,
            tmp_path,
            ExecutionContext(cancellation=token),
        )
        deadline = time.monotonic() + 3
        while not signal.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert signal.exists()
        token.cancel()
        with pytest.raises(WorkflowError, match="cancelled"):
            future.result(3)
    with pytest.raises(WorkflowError, match="deadline_expired"):
        _execute(
            AsyncSubprocessExecutor(),
            argv,
            tmp_path,
            ExecutionContext(deadline=datetime.now(UTC) - timedelta(seconds=1)),
        )
    with pytest.raises(WorkflowError, match="cancelled"):
        _execute(AsyncSubprocessExecutor(), argv, tmp_path, ExecutionContext(cancellation=token))


def test_gate_failure_is_retained_and_unsupported_family_never_initializes_git(tmp_path):
    class FailingExecutor:
        async def execute(self, request):
            return ProgramResult(
                ProgramState.FAILED, 17, b"", b"rejected by actual gate", timedelta()
            )

    directory = tmp_path / "_af_engineering"
    directory.mkdir()
    engineer = GitTemplateEngineer(tmp_path, FailingExecutor())
    with pytest.raises(WorkflowError, match="unsupported_engineering_family"):
        engineer.engineer("unconfined-family", ExecutionContext(), route_history=("exhausted",))
    with pytest.raises(WorkflowError, match="gate_failed:tests"):
        engineer._gate("tests", ("python",), tmp_path, ExecutionContext())
    assert "rejected by actual gate" in (directory / "tests.json").read_text()
    with pytest.raises(WorkflowError, match="git_failed"):
        engineer._git(tmp_path, ExecutionContext(), "status")
    with pytest.raises(WorkflowError, match="gate_failed:security"):
        engineer._record_gate("security", False, {"unexpected-egress": True}, directory)
    assert not FiniteTemplateReviewer().review(b'__import__("os").system("false")', ())


def test_interrupted_engineering_claim_is_bounded_without_automatic_reexecution(tmp_path):
    from af.domain.workflow_components import reference_workflow_components
    from af.infrastructure.workflows import SqlAlchemyWorkflowRepository

    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    catalogue = SqlAlchemyEngineeringCatalogue(database, authentication_key=b"k" * 32)
    request = EngineeringRequest(texts=("one",))
    assert catalogue.claim(request.family, "interrupted-owner")
    registry = ProcedureRegistry()
    service = LocalEngineeringService(
        catalogue,
        GitTemplateEngineer(tmp_path, AsyncSubprocessExecutor()),
        SqlAlchemyWorkflowRepository(database, authentication_key=b"k" * 32),
        reference_workflow_components()[1],
        wait_seconds=0.02,
        route_probe=InstalledEngineeringRouteProbe(lambda: UnknownClassifier(), lambda: registry),
    )
    with pytest.raises(WorkflowError, match="engineering_in_progress"):
        service.complete(request, ExecutionContext())
    assert catalogue.status(request.family) == "engineering"
    assert not (tmp_path / ".git").exists()
    database.close()
