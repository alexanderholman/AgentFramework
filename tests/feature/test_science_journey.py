"""Science calculations pass through the shared CLI, REST and reuse pipeline."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.interfaces.cli import create_cli
from af.interfaces.http import create_http_app


def test_math_and_physics_are_verified_through_cli_rest_and_reuse(application):
    prompts = (
        "math add a=1/3 b=1/6",
        "physics kinetic_energy mass=2 kg speed=3 m/s",
        "math root value=2 degree=2",
        "math mean values=1 m,200 cm,3 m",
    )
    for prompt in prompts:
        cli = CliRunner().invoke(create_cli(application), ["ask", prompt])
        assert cli.exit_code == 0, cli.stdout
        cli_result = json.loads(cli.stdout)
        assert cli_result["reason_code"] == "science_verified"
        with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
            response = client.post("/api/v1/tasks", json={"prompt": prompt})
        assert response.status_code == 201, response.text
        task = response.json()
        assert task["route"] == "science-calculate"
        assert task["verified"] is True
        assert json.loads(task["output"]) == cli_result
        assert application.traces.get(task["trace_id"]).reused is True
