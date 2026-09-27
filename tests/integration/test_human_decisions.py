from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.application.human_decisions import HumanDecisionService
from af.bootstrap import create_application
from af.config import Settings
from af.framework.context import ExecutionContext, Principal
from af.framework.human_decisions import HumanDecisionError
from af.interfaces.console import create_cli
from af.interfaces.http import create_http_app


def test_cli_pause_rest_restart_resume_executes_only_pinned_choice(tmp_path: Path) -> None:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}")
    runner = CliRunner()
    decision_file = tmp_path / "decision.json"
    app = create_application(settings=settings)
    try:
        result = runner.invoke(
            create_cli(app),
            [
                "resolve",
                "request",
                'count characters in "strawberry"',
                "--output",
                str(decision_file),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(decision_file.read_text())
        assert payload["kind"] == "NeedsClarification"
        assert decision_file.stat().st_mode & 0o077 == 0
        assert payload["response_secret"] not in result.output
        assert not app.tasks.list()
        with sqlite3.connect(tmp_path / "state.db") as db:
            row = db.execute("SELECT payload FROM af_human_decisions").fetchone()[0]
        assert payload["response_secret"] not in row
        assert "count characters in" not in row
    finally:
        app.shutdown()
    app = create_application(settings=settings)
    try:
        choice = next(c["choice_id"] for c in payload["choices"] if c["label"] == "Count 'r'")
        with TestClient(create_http_app(app), base_url="http://localhost") as client:
            response = client.post(
                "/api/v1/decisions/respond",
                json={
                    "decision_id": payload["decision_id"],
                    "response_secret": payload["response_secret"],
                    "choice_id": choice,
                },
            )
            assert response.status_code == 200, response.text
            completed = response.json()
            assert completed["kind"] == "Completed"
            assert completed["result"]["output"] == "3"
            assert completed["result"]["verified"] is True
            assert (
                client.post(
                    "/api/v1/decisions/respond",
                    json={
                        "decision_id": payload["decision_id"],
                        "response_secret": payload["response_secret"],
                        "choice_id": choice,
                    },
                ).status_code
                == 409
            )
    finally:
        app.shutdown()


@pytest.fixture
def machine(tmp_path: Path):
    app = create_application(settings=Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}"))
    try:
        yield app, app.container.resolve(HumanDecisionService), tmp_path / "state.db"
    finally:
        app.shutdown()


def test_confirmation_revocation_forgery_and_authority(machine) -> None:
    _app, service, path = machine
    context = ExecutionContext(principal=Principal("alice", "human"), tenant_id="one")
    decision = service.submit("confirm: hello", context)
    assert decision["kind"] == "NeedsConfirmation"
    did, secret = decision["decision_id"], decision["response_secret"]
    assert service.inspect(did, context)["choices"]
    for provided, choice, authority in (
        ("forged", "confirm", context),
        (secret, "other", context),
        (secret, "confirm", ExecutionContext(principal=Principal("bob", "human"), tenant_id="one")),
        (
            secret,
            "confirm",
            ExecutionContext(principal=Principal("alice", "human"), tenant_id="two"),
        ),
    ):
        with pytest.raises(HumanDecisionError):
            service.resume(did, provided, choice, authority)
    assert service.revoke(did, context)["reason"] == "revoked"
    with pytest.raises(HumanDecisionError):
        service.resume(did, secret, "confirm", context)
    # Merely resetting a state column cannot resurrect authenticated revoked state.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE af_human_decisions SET status='pending' WHERE id=?", (did,))
    with pytest.raises(HumanDecisionError):
        service.resume(did, secret, "confirm", context)


def test_concurrent_responses_have_exactly_one_winner(machine) -> None:
    _app, service, _ = machine
    context = ExecutionContext()
    result = service.submit("confirm: hello", context)

    def respond(_: int) -> object:
        try:
            return service.resume(
                result["decision_id"], result["response_secret"], "confirm", context
            )
        except HumanDecisionError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(respond, range(8)))
    assert sum(answer is not None for answer in answers) == 1


def test_expiry_policy_cancellation_tampering_and_unavailable_routes(machine) -> None:
    _app, service, path = machine
    context = ExecutionContext()
    result = service.submit("confirm: hello", context)
    original_policy = service._policy
    service._policy = "f" * 64
    with pytest.raises(HumanDecisionError):
        service.resume(result["decision_id"], result["response_secret"], "confirm", context)
    service._policy = original_policy
    service._clock = lambda: result["expires_at"] + 1
    with pytest.raises(HumanDecisionError):
        service.resume(result["decision_id"], result["response_secret"], "confirm", context)
    service._clock = lambda: 1.0
    context.cancellation.cancel()
    with pytest.raises(HumanDecisionError):
        service.submit("confirm: hello", context)
    context = ExecutionContext()
    for prompt in ("unknown", "confirm: generate anything"):
        assert service.submit(prompt, context)["kind"] == "NoRoute"
    assert service.submit('count characters in "abcdefghijklmnopq"', context)["kind"] == "Denied"
    assert service.submit("confirm: what time is it", context)["kind"] == "Denied"
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE af_human_decisions SET payload='{}' WHERE id=?", (result["decision_id"],)
        )
    with pytest.raises(HumanDecisionError):
        service.inspect(result["decision_id"], context)


def test_failed_execution_is_not_replayed(machine, monkeypatch) -> None:
    app, service, path = machine
    context = ExecutionContext()
    result = service.submit("confirm: hello", context)

    def fail(*args, **kwargs):
        raise RuntimeError("interrupted")

    monkeypatch.setattr(app.agent, "handle_classification", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        service.resume(result["decision_id"], result["response_secret"], "confirm", context)
    with pytest.raises(HumanDecisionError):
        service.resume(result["decision_id"], result["response_secret"], "confirm", context)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM af_human_decisions").fetchone()[0] == "inconclusive"


def test_signed_pending_row_rollback_cannot_replay_completed_response(machine) -> None:
    _app, service, path = machine
    context = ExecutionContext()
    decision = service.submit("confirm: hello", context)
    with sqlite3.connect(path) as db:
        original = db.execute(
            "SELECT payload,signature,status FROM af_human_decisions WHERE id=?",
            (decision["decision_id"],),
        ).fetchone()
    service.resume(decision["decision_id"], decision["response_secret"], "confirm", context)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE af_human_decisions SET payload=?,signature=?,status=? WHERE id=?",
            (*original, decision["decision_id"]),
        )
    with pytest.raises(HumanDecisionError):
        service.resume(decision["decision_id"], decision["response_secret"], "confirm", context)


def test_expiry_during_claim_prevents_execution(machine, monkeypatch) -> None:
    app, service, _ = machine
    context = ExecutionContext()
    decision = service.submit("confirm: hello", context)
    calls = []
    original_claim = service._repository.claim

    def delayed_claim(pending):
        original_claim(pending)
        service._clock = lambda: decision["expires_at"] + 1

    monkeypatch.setattr(service._repository, "claim", delayed_claim)
    monkeypatch.setattr(app.agent, "handle_classification", lambda *a, **kw: calls.append(True))
    with pytest.raises(HumanDecisionError):
        service.resume(decision["decision_id"], decision["response_secret"], "confirm", context)
    assert calls == []


def test_human_resume_binds_exact_authority_and_narrowed_expiry(machine, monkeypatch) -> None:
    from af.framework.context import maybe_current_context

    app, service, _ = machine
    context = ExecutionContext(principal=Principal("alice", "human"), tenant_id="tenant-a")
    decision = service.submit("confirm: hello", context)
    observed = []
    original_handle = app.agent.handle_classification

    def observe(*args, **kwargs):
        observed.append(maybe_current_context())
        return original_handle(*args, **kwargs)

    monkeypatch.setattr(app.agent, "handle_classification", observe)
    service.resume(decision["decision_id"], decision["response_secret"], "confirm", context)
    assert observed[0] is not None
    assert observed[0].principal == context.principal
    assert observed[0].tenant_id == context.tenant_id
    assert observed[0].deadline.timestamp() == pytest.approx(
        decision["expires_at"], rel=0, abs=1e-6
    )


def test_cli_and_http_inspect_respond_revoke_and_validation(machine, tmp_path) -> None:
    app, _service, _path = machine
    runner = CliRunner()
    cli = create_cli(app)
    decision_file = tmp_path / "confirmation.json"
    issued = runner.invoke(
        cli, ["resolve", "request", "confirm: hello", "--output", str(decision_file)]
    )
    assert issued.exit_code == 0, issued.output
    payload = json.loads(decision_file.read_text())
    read = runner.invoke(cli, ["resolve", "inspect", payload["decision_id"]])
    assert read.exit_code == 0
    assert "response_secret" not in json.loads(read.output)
    completed = runner.invoke(
        cli, ["resolve", "respond", "--decision", str(decision_file), "--choice", "confirm"]
    )
    assert completed.exit_code == 0, completed.output
    assert json.loads(completed.output)["result"]["output"] == "Hello, world!"
    assert (
        runner.invoke(
            cli, ["resolve", "respond", "--decision", str(decision_file), "--choice", "confirm"]
        ).exit_code
        == 2
    )
    invalid = tmp_path / "invalid.json"
    invalid.write_text("[]")
    assert (
        runner.invoke(
            cli, ["resolve", "respond", "--decision", str(invalid), "--choice", "confirm"]
        ).exit_code
        == 2
    )
    next_file = tmp_path / "second.json"
    assert (
        runner.invoke(
            cli, ["resolve", "request", "confirm: hello", "--output", str(next_file)]
        ).exit_code
        == 0
    )
    pending = json.loads(next_file.read_text())
    assert runner.invoke(cli, ["resolve", "revoke", pending["decision_id"]]).exit_code == 0
    assert (
        runner.invoke(
            cli, ["resolve", "request", "confirm: hello", "--output", str(next_file)]
        ).exit_code
        == 2
    )
    with TestClient(create_http_app(app), base_url="http://localhost") as client:
        submitted = client.post("/api/v1/decisions", json={"prompt": "confirm: hello"})
        assert submitted.status_code == 200
        did = submitted.json()["decision_id"]
        assert client.get(f"/api/v1/decisions/{did}").status_code == 200
        assert client.post(f"/api/v1/decisions/{did}/revoke").status_code == 200
        assert client.get(f"/api/v1/decisions/{did}").status_code == 409
        assert (
            client.post(
                "/api/v1/decisions", json={"prompt": "confirm: hello", "expires_in": True}
            ).status_code
            == 422
        )


def test_changed_procedure_and_unverified_outcome_cannot_complete(machine, monkeypatch) -> None:
    from dataclasses import replace

    app, service, _ = machine
    context = ExecutionContext()
    decision = service.submit("confirm: hello", context)
    original_handle = app.agent.handle_classification
    monkeypatch.setattr(
        app.agent,
        "handle_classification",
        lambda *args, **kwargs: replace(original_handle(*args, **kwargs), verified=False),
    )
    with pytest.raises(HumanDecisionError):
        service.resume(decision["decision_id"], decision["response_secret"], "confirm", context)
    decision = service.submit("confirm: hello", context)
    original_choice = service._choice
    monkeypatch.setattr(
        service,
        "_choice",
        lambda *args, **kwargs: original_choice(*args, **kwargs).model_copy(
            update={"label": "changed"}
        ),
    )
    with pytest.raises(HumanDecisionError):
        service.resume(decision["decision_id"], decision["response_secret"], "confirm", context)
    with pytest.raises(HumanDecisionError):
        service.submit("confirm: hello", context, expires_in=True)
    with pytest.raises(HumanDecisionError):
        service.resume(decision["decision_id"], "\ud800", "confirm", context)


def test_repository_private_creation_duplicate_wrong_key_and_invalid_records(
    machine, tmp_path
) -> None:
    from af.infrastructure.human_decisions import SQLiteHumanDecisionRepository

    _app, service, _path = machine
    context = ExecutionContext()
    decision = service.submit("confirm: hello", context)
    pending = service._repository.get(decision["decision_id"])
    path = tmp_path / "isolated.db"
    repository = SQLiteHumanDecisionRepository(path, b"k" * 32)
    assert path.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError):
        SQLiteHumanDecisionRepository(path, b"short")
    with pytest.raises(HumanDecisionError):
        repository.get("bad id")
    repository.create(pending)
    with pytest.raises(HumanDecisionError):
        repository.create(pending)
    with pytest.raises(HumanDecisionError):
        SQLiteHumanDecisionRepository(path, b"z" * 32).get(pending.decision_id)
    with pytest.raises(ValueError):
        repository.finish(pending.decision_id, "pending")
    with pytest.raises(HumanDecisionError):
        repository.finish(pending.decision_id, "completed")
    with pytest.raises(HumanDecisionError):
        repository.claim(pending.model_copy(update={"policy_digest": "a" * 64}))
    repository.claim(pending)
    repository.finish(pending.decision_id, "inconclusive")
    with pytest.raises(HumanDecisionError):
        repository.finish(pending.decision_id, "completed")
    # A correctly authenticated but structurally invalid payload must also fail closed.
    with sqlite3.connect(path) as db:
        signature = repository._signature("{}")
        db.execute(
            "UPDATE af_human_decisions SET payload='{}',signature=?,status='pending'", (signature,)
        )
        db.execute(
            "UPDATE af_human_decision_history SET payload='{}',signature=?,status='pending'",
            (signature,),
        )
    with pytest.raises(HumanDecisionError):
        repository.get(pending.decision_id)


def test_pending_contract_rejects_unknown_schema_unbounded_expiry_and_duplicate_choices(machine):
    from pydantic import ValidationError

    from af.framework.human_decisions import PendingHumanDecision

    _app, service, _path = machine
    issued = service.submit("confirm: hello", ExecutionContext())
    pending = service._repository.get(issued["decision_id"])
    for changes in (
        {"schema_version": "af.human-decision/999"},
        {"expires_at": pending.created_at + 3601},
        {"kind": "Completed"},
        {"choices": pending.choices * 2},
    ):
        with pytest.raises(ValidationError):
            PendingHumanDecision.model_validate({**pending.model_dump(), **changes})
