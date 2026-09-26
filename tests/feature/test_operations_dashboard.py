from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.interfaces.cli import create_cli
from af.interfaces.http import create_http_app

runner = CliRunner()


def _without_observation_time(payload: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in payload.items() if key != "observed_at"}


def test_operations_dashboard_has_shared_redacted_html_rest_and_terminal_views(application) -> None:  # type: ignore[no-untyped-def]
    created = application.tasks.create("HELLO")
    trace_count = len(application.traces.list())
    app = create_http_app(application)

    with TestClient(app, base_url="http://localhost") as client:
        rest_response = client.get("/api/v1/operations")
        html_response = client.get("/operations")
        schema = client.get("/openapi.json").json()

    terminal_response = runner.invoke(create_cli(application), ["operations", "--json"])

    assert rest_response.status_code == 200
    rest_payload = rest_response.json()
    assert rest_payload["schema_version"] == "1"
    assert rest_payload["runtime"] == {
        "lifecycle_state": "serving",
        "liveness": "live",
        "readiness": "unknown",
        "reason": "component readiness has not been observed",
    }
    assert rest_payload["activity"]["observed"] == 1
    assert rest_payload["activity"]["available"] is True
    assert rest_payload["recent_tasks"][0]["id"] == created.id
    assert set(rest_payload["recent_tasks"][0]) == {
        "id",
        "status",
        "route",
        "tier",
        "verified",
        "reused",
        "created_at",
        "updated_at",
    }
    serialized = json.dumps(rest_payload)
    assert created.prompt not in serialized
    assert created.output not in serialized
    assert created.trace_id not in serialized

    assert html_response.status_code == 200
    assert html_response.text.count("<main ") == 1
    assert '<section lang="en" dir="ltr" aria-labelledby="operations-title">' in html_response.text
    assert '<table aria-labelledby="recent-title">' in html_response.text
    assert "Operations" in html_response.text
    assert created.id in html_response.text
    assert created.prompt not in html_response.text
    assert created.output not in html_response.text
    assert created.trace_id not in html_response.text

    assert terminal_response.exit_code == 0
    terminal_payload = json.loads(terminal_response.stdout)
    assert _without_observation_time(terminal_payload) == _without_observation_time(rest_payload)

    operation = schema["paths"]["/api/v1/operations"]["get"]
    assert operation["operationId"] == "get_operations_api_v1_operations_get"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OperationsResponse"
    }
    assert len(application.traces.list()) == trace_count
    assert len(application.tasks.list()) == 1


def test_operations_terminal_plain_view_is_stable_and_headless(application) -> None:  # type: ignore[no-untyped-def]
    result = runner.invoke(create_cli(application), ["operations"])

    assert result.exit_code == 0
    assert result.stdout == (
        "AF Operations\n"
        "Lifecycle: serving\n"
        "Liveness: live\n"
        "Readiness: unknown (component readiness has not been observed)\n"
        "Activity window: 0/25 tasks\n"
        "Verified: 0 | Reused: 0\n"
        "Recent tasks: none\n"
    )
