from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from test_local_engineering import repository, service_factory
from typer.testing import CliRunner

from af.framework.context import ExecutionContext, use_context
from af.framework.engineering import EngineeringRequest
from af.framework.self_development_execution import LocalDevelopmentUnit
from af.infrastructure.database import Database
from af.infrastructure.self_development import create_local_development_controller
from af.interfaces.self_development import register_self_development_commands


def configure(tmp_path):
    import subprocess

    root = repository(tmp_path)
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"]).decode().strip()
    unit = LocalDevelopmentUnit(
        candidate_id="issue:local-lines",
        expected_head=head,
        repository="local/project",
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
        request=EngineeringRequest(texts=("one\n\ntwo", "three")),
    )
    path = tmp_path / "unit.json"
    path.write_text(unit.model_dump_json())
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    engineering = service_factory(database, root, authentication_key=b"k" * 32)
    controller = create_local_development_controller(
        database, root, path, authentication_key=b"k" * 32, engineering=engineering
    )
    return root, path, database, controller


def cli(controller):
    import typer

    result = typer.Typer()

    @result.callback()
    def entry():
        pass

    register_self_development_commands(result, lambda _: controller)
    return result


def test_configured_public_plan_execute_restart_and_fresh_reuse(tmp_path):
    root, path, database, controller = configure(tmp_path)
    runner = CliRunner()
    with use_context(ExecutionContext()):
        preview = runner.invoke(cli(controller), ["self-develop"])
        assert preview.exit_code == 0, preview.output
        plan = json.loads(preview.stdout)
        assert plan["plan"]["route"] == "engineer_gap"
        assert not (root / ".git" / "af").exists()
        executed = runner.invoke(
            cli(controller), ["self-develop", "--execute", "--plan-digest", plan["plan_digest"]]
        )
        assert executed.exit_code == 0, executed.output
        first = json.loads(executed.stdout)
        assert first["completion"]["output"] == 3
        assert first["completion"]["engineered"]
        assert all(gate["passed"] for gate in first["completion"]["admission"]["gates"])
    database.close()
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    restarted = create_local_development_controller(
        database,
        root,
        path,
        authentication_key=b"k" * 32,
        engineering=service_factory(database, root, authentication_key=b"k" * 32),
    )
    with use_context(ExecutionContext()):
        repeated = runner.invoke(
            cli(restarted), ["self-develop", "--execute", "--plan-digest", plan["plan_digest"]]
        )
        assert repeated.exit_code == 0, repeated.output
        assert json.loads(repeated.stdout) == first
        fresh = json.loads(runner.invoke(cli(restarted), ["self-develop"]).stdout)
        assert fresh["plan_digest"] != plan["plan_digest"]
        result = runner.invoke(
            cli(restarted), ["self-develop", "--execute", "--plan-digest", fresh["plan_digest"]]
        )
        assert result.exit_code == 0, result.output
        assert not json.loads(result.stdout)["completion"]["engineered"]
    database.close()


def test_changed_head_or_host_unit_never_executes(tmp_path):
    import subprocess

    root, _path, database, controller = configure(tmp_path)
    context = ExecutionContext()
    prepared = controller.plan(context=context)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-m", "drift"],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ValueError, match=r"changed|stale"):
        controller.execute(prepared.plan_digest, context)
    assert not (root / ".git" / "af").exists()
    database.close()


def test_expired_unit_and_cancellation_never_executes(tmp_path):
    root, path, database, controller = configure(tmp_path)
    context = ExecutionContext()
    prepared = controller.plan(context=context)
    unit = LocalDevelopmentUnit.model_validate_json(path.read_bytes())
    path.write_text(
        unit.model_copy(
            update={
                "issued_at": datetime.now(UTC) - timedelta(minutes=90),
                "expires_at": datetime.now(UTC) - timedelta(hours=1),
            }
        ).model_dump_json()
    )
    with pytest.raises(ValueError, match=r"changed|stale"):
        controller.execute(prepared.plan_digest, context)
    context.cancellation.cancel()
    with pytest.raises(ValueError, match="cancelled"):
        controller.execute(prepared.plan_digest, context)
    assert not (root / ".git" / "af").exists()
    database.close()


def test_interrupted_claim_and_wrong_principal_never_replay_effects(tmp_path):
    from af.framework.context import Principal
    from af.framework.workflows import WorkflowError
    from af.infrastructure.self_development import SqlAlchemyDevelopmentLedger

    root, _path, database, controller = configure(tmp_path)
    owner = ExecutionContext(principal=Principal("owner", "user"))
    prepared = controller.plan(context=owner)
    with pytest.raises(ValueError, match="authority changed"):
        controller.execute(
            prepared.plan_digest, ExecutionContext(principal=Principal("stranger", "user"))
        )
    ledger = SqlAlchemyDevelopmentLedger(database, b"k" * 32)
    assert ledger.claim(prepared.plan_digest)
    assert not ledger.claim(prepared.plan_digest)
    with pytest.raises(WorkflowError, match="requires recovery"):
        controller.execute(prepared.plan_digest, owner)
    assert not (root / ".git" / "af").exists()
    database.close()


def test_durable_receipt_authentication_and_event_order_fail_closed(tmp_path):
    from sqlalchemy import text

    from af.framework.workflows import WorkflowError
    from af.infrastructure.self_development import SqlAlchemyDevelopmentLedger

    _root, _path, database, controller = configure(tmp_path)
    prepared = controller.plan(context=ExecutionContext())
    ledger = SqlAlchemyDevelopmentLedger(database, b"k" * 32)
    ledger.save(prepared)
    assert ledger.load(prepared.plan_digest) == prepared
    with pytest.raises(ValueError, match="unknown"):
        ledger.load("sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="key"):
        SqlAlchemyDevelopmentLedger(database, b"short")
    with pytest.raises(WorkflowError, match="authentication"):
        SqlAlchemyDevelopmentLedger(database, b"z" * 32).load(prepared.plan_digest)
    with pytest.raises(WorkflowError, match="identity conflict"):
        ledger.save(prepared.model_copy(update={"authority_digest": "f" * 64}))
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE af_development_events SET payload='{}'"))
    with pytest.raises(WorkflowError, match="authentication"):
        ledger.load(prepared.plan_digest)
    with database.engine.begin() as connection:
        connection.execute(text("UPDATE af_development_events SET kind='completed'"))
    with pytest.raises(WorkflowError, match="event order"):
        ledger.load(prepared.plan_digest)
    database.close()


def test_read_only_snapshot_cannot_become_execution_authority(tmp_path):
    from af.application.self_development import SelfDevelopmentController

    root, _path, database, configured = configure(tmp_path)
    prepared = configured.plan(context=ExecutionContext())
    path = tmp_path / "observation.json"
    path.write_text(prepared.inputs.model_dump_json())
    service = SelfDevelopmentController()
    runner = CliRunner()
    with use_context(ExecutionContext()):
        result = runner.invoke(cli(service), ["self-develop", "--snapshot", str(path)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["plan"]["allowed_writes"] == []
        for arguments in (
            [],
            ["--execute"],
            ["--execute", "--plan-digest", prepared.plan_digest],
            ["--execute", "--snapshot", str(path), "--plan-digest", prepared.plan_digest],
            ["--plan-digest", prepared.plan_digest],
        ):
            assert runner.invoke(cli(service), ["self-develop", *arguments]).exit_code == 2
        path.write_bytes(b"x" * 1_048_577)
        assert runner.invoke(cli(service), ["self-develop", "--snapshot", str(path)]).exit_code == 2
    assert not (root / ".git" / "af").exists()
    database.close()


def test_host_configuration_and_final_source_guard_are_fail_closed(tmp_path):
    from af.framework.workflows import WorkflowError
    from af.infrastructure.local_engineering import GitTemplateEngineer
    from af.infrastructure.programs import AsyncSubprocessExecutor

    root, path, database, controller = configure(tmp_path)
    prepared = controller.plan(context=ExecutionContext())
    unit = prepared.inputs.unit
    assert unit is not None
    with pytest.raises(ValueError, match="timezone"):
        LocalDevelopmentUnit.model_validate(unit.model_dump() | {"issued_at": datetime.now()})
    with pytest.raises(ValueError, match="one hour"):
        LocalDevelopmentUnit.model_validate(
            unit.model_dump() | {"expires_at": unit.issued_at + timedelta(hours=2)}
        )
    engineer = GitTemplateEngineer(root, AsyncSubprocessExecutor())
    with pytest.raises(WorkflowError, match="head_changed"):
        engineer.engineer(
            unit.request.family,
            ExecutionContext(),
            route_history=("trusted-test",),
            expected_source_head="f" * 40,
        )
    assert not (root / ".git" / "af").exists()
    path.write_bytes(b"x" * 262_145)
    with pytest.raises(ValueError, match="input bound"):
        controller.plan(context=ExecutionContext())
    path.unlink()
    path.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="regular host"):
        controller.plan(context=ExecutionContext())
    database.close()


def test_installed_composition_configured_cycle_survives_restart(tmp_path):
    from af.bootstrap import create_application
    from af.config import Settings
    from af.interfaces.console import create_cli

    root, path, database, _controller = configure(tmp_path)
    database.close()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'installed.db'}",
        engineering_repository=str(root),
        self_development_unit=str(path),
    )
    application = create_application(settings=settings)
    runner = CliRunner()
    try:
        cli_app = create_cli(application)
        planned = runner.invoke(cli_app, ["self-develop"])
        assert planned.exit_code == 0, planned.output
        digest = json.loads(planned.stdout)["plan_digest"]
        executed = runner.invoke(cli_app, ["self-develop", "--execute", "--plan-digest", digest])
        assert executed.exit_code == 0, executed.output
    finally:
        application.shutdown()
    restarted = create_application(settings=settings)
    try:
        cli_app = create_cli(restarted)
        resumed = runner.invoke(cli_app, ["self-develop", "--execute", "--plan-digest", digest])
        assert resumed.exit_code == 0, resumed.output
        assert json.loads(resumed.stdout) == json.loads(executed.stdout)
        direct = runner.invoke(cli_app, ["ask", "count nonblank lines: one\n\ntwo"])
        assert direct.exit_code == 0 and direct.stdout.strip() == "2", direct.output
    finally:
        restarted.shutdown()
