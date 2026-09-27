"""Optional observation and admin commands authenticate before shared use cases."""

import json
from pathlib import Path

import typer
from typer.testing import CliRunner

from af.application.admin import AdminService
from af.application.authentication import AuthenticationService
from af.application.observability_products import InspectorService, MetricsService
from af.framework.admin import AdminAction, AdminActionResult, AdminResource, AdminRow
from af.framework.context import Principal
from af.framework.observability_products import (
    InspectorKind,
    InspectorPolicy,
    MetricDefinition,
    MetricKind,
)
from af.infrastructure.authentication import SQLiteAuthenticationRepository
from af.infrastructure.observability_products import SQLiteInspectorStore, SQLiteMetricsStore
from af.interfaces.observability_products import (
    ObservabilityProducts,
    register_observability_commands,
)


def test_observation_cli_authenticates_and_dispatches_installed_admin_callbacks(
    tmp_path: Path,
) -> None:
    auth = AuthenticationService(SQLiteAuthenticationRepository(tmp_path / "auth"))
    scopes = ("af:observability:read", "af:observability:write", "health:read", "queue:write")
    token = auth.issue(Principal("operator", "user", scopes=scopes), tenant_id="a")
    credential = tmp_path / "credential"
    credential.write_text(json.dumps({"secret": token.secret}))
    inspector = InspectorService(
        SQLiteInspectorStore(tmp_path / "inspect"),
        InspectorPolicy(("task.completed",)),
        tenant_id="a",
    )
    metrics = MetricsService(
        SQLiteMetricsStore(tmp_path / "metrics"),
        (MetricDefinition("tasks", MetricKind.COUNTER),),
        tenant_id="a",
    )
    calls: list[tuple[str, object]] = []

    def query(principal: Principal, limit: int) -> tuple[AdminRow, ...]:
        calls.append(("query", principal.subject_id))
        return (AdminRow("worker", "ready"),)

    def command(principal: Principal, parameters: object) -> AdminActionResult:
        calls.append(("command", parameters))
        return AdminActionResult("queued", "work-1")

    admin = AdminService(
        (
            AdminResource(
                "health", "health:read", query, (AdminAction("restart", "queue:write", command),)
            ),
        )
    )
    products = ObservabilityProducts(inspector, metrics, admin)
    cli = typer.Typer()
    register_observability_commands(cli, lambda ctx: products, lambda ctx: auth, lambda ctx: "a")
    runner = CliRunner()
    inspector.collect(
        InspectorKind.JOB, "task.completed", event_id="job-1", fields={"status": "ok"}
    )
    metrics.record("tasks", 1, {})
    for action in ("inspector", "metrics"):
        result = runner.invoke(cli, ["observe", action, "--credential", str(credential)])
        assert result.exit_code == 0, result.output
        assert len(json.loads(result.stdout)) == 1
    assert (
        runner.invoke(
            cli, ["observe", "admin-query", "health", "--credential", str(credential)]
        ).exit_code
        == 0
    )
    request = tmp_path / "request"
    request.write_text('{"worker":"one"}')
    args = [
        "observe",
        "admin-action",
        "health",
        "restart",
        "--request",
        str(request),
        "--credential",
        str(credential),
    ]
    assert runner.invoke(cli, args).exit_code == 2
    assert len(calls) == 1
    assert runner.invoke(cli, [*args, "--confirm"]).exit_code == 0
    assert len(calls) == 2
    for product in ("inspector", "metrics"):
        assert (
            runner.invoke(
                cli, ["observe", "purge", product, "--credential", str(credential)]
            ).exit_code
            == 2
        )
        assert (
            runner.invoke(
                cli, ["observe", "purge", product, "--credential", str(credential), "--confirm"]
            ).exit_code
            == 0
        )
    auth.revoke(token.token_id, tenant_id="a")
    assert (
        runner.invoke(cli, ["observe", "metrics", "--credential", str(credential)]).exit_code == 2
    )
    assert token.secret not in result.output
