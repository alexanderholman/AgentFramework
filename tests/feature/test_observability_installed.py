"""Installed CLI/HTTP tasks share optional observation without changing task semantics."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.application.authentication import AuthenticationService
from af.bootstrap import create_application
from af.bootstrap_observability import with_reference_admin
from af.config import Settings
from af.framework.context import Principal
from af.framework.observability_products import ObservabilityDenied
from af.interfaces.console import create_cli
from af.interfaces.http import create_http_app
from af.interfaces.observability_products import ObservabilityProducts


@pytest.mark.parametrize(
    "inspector,metrics", [(False, False), (True, False), (False, True), (True, True)]
)
def test_independent_products_observe_installed_cli_and_http_tasks(
    tmp_path: Path, inspector: bool, metrics: bool
) -> None:
    app = create_application(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'state.db'}",
            inspector_enabled=inspector,
            metrics_enabled=metrics,
        )
    )
    try:
        actor = Principal("observer", "user", scopes=("af:observability:read",))
        auth = app.container.resolve(AuthenticationService)
        token = auth.issue(actor, tenant_id="local")
        credential = tmp_path / "credential"
        credential.write_text(json.dumps({"secret": token.secret}))
        prompt = "how many rs in strawberry"
        result = CliRunner().invoke(create_cli(app), ["tasks", "create", prompt])
        assert result.exit_code == 0, result.output
        with TestClient(create_http_app(app), base_url="http://127.0.0.1") as client:
            response = client.post("/api/v1/tasks", json={"prompt": prompt})
            assert response.status_code == 201, response.text
        assert len(app.tasks.list()) == 2
        products = app.container.resolve(ObservabilityProducts)
        assert (products.inspector is not None) == inspector
        assert (products.metrics is not None) == metrics
        for name, enabled in (("inspector", inspector), ("metrics", metrics)):
            query = CliRunner().invoke(
                create_cli(app), ["observe", name, "--credential", str(credential)]
            )
            assert query.exit_code == (0 if enabled else 2), query.output
            if enabled:
                assert prompt not in query.stdout
                assert len(json.loads(query.stdout)) == 2
        if products.metrics is not None:
            assert (
                next(
                    card
                    for card in products.metrics.cards(actor)
                    if card.name == "af.task.completed"
                ).total
                == 2
            )
        if products.inspector is not None:
            assert len(products.inspector.entries(actor)) == 2
    finally:
        app.shutdown()


def test_reference_admin_queries_and_delete_use_existing_application_service(
    tmp_path: Path,
) -> None:
    app = create_application(settings=Settings(database_url=f"sqlite:///{tmp_path / 'state.db'}"))
    try:
        task = app.tasks.create("hello")
        products = with_reference_admin(
            app.container.resolve(ObservabilityProducts), app.tasks, app.operations
        )
        assert products.admin is not None
        admin = products.admin
        reader = Principal("reader", "user", scopes=("af:read",))
        writer = Principal("writer", "user", scopes=("af:write",))
        row = admin.query(reader, "tasks")[0]
        assert row.reference == task.id and "prompt" not in dict(row.attributes)
        assert admin.query(reader, "health")[0].reference == "application"
        with pytest.raises(ObservabilityDenied):
            admin.action(reader, "tasks", "delete", {"task_id": task.id}, confirmed=True)
        with pytest.raises(ObservabilityDenied):
            admin.action(writer, "tasks", "delete", {"task_id": task.id})
        with pytest.raises(ValueError):
            admin.action(writer, "tasks", "delete", {"other": task.id}, confirmed=True)
        assert app.tasks.get(task.id) == task
        assert (
            admin.action(writer, "tasks", "delete", {"task_id": task.id}, confirmed=True).status
            == "completed"
        )
        assert app.tasks.list() == []
    finally:
        app.shutdown()
