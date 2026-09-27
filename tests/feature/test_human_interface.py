"""Thin CLI and HTTP adapters share the observed-signal boundary."""

from __future__ import annotations

import json

import typer
from fastapi import FastAPI
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.application.human_interpreter import HumanInterpreter
from af.infrastructure.ideon_reference import create_reference_ideon_runtime
from af.interfaces.human_interpreter import (
    create_human_interpreter_router,
    register_human_interpreter_commands,
)


def test_text_and_json_cli_http_have_identical_semantic_records():
    service = HumanInterpreter(create_reference_ideon_runtime().controller)
    cli = typer.Typer()
    register_human_interpreter_commands(cli, lambda _: service)
    metadata = {
        "text": "sort integers [3,1]",
        "source_id": "fixture:1",
        "source_kind": "user",
        "rights": "user-provided",
        "received_at": "2026-01-01T00:00:00+00:00",
    }
    runner = CliRunner()
    text = runner.invoke(
        cli,
        [
            "human",
            "interpret",
            metadata["text"],
            "--source-id",
            "fixture:1",
            "--received-at",
            metadata["received_at"],
        ],
    )
    assert text.exit_code == 0, text.stdout
    structured = runner.invoke(cli, ["human", "interpret-json", json.dumps(metadata)])
    assert structured.exit_code == 0, structured.stdout
    assert json.loads(structured.stdout) == json.loads(text.stdout)
    app = FastAPI()
    app.include_router(create_human_interpreter_router(service))
    with TestClient(app) as client:
        response = client.post("/api/v1/human/interpret", json=metadata)
        assert response.status_code == 200, response.text
        assert response.json() == json.loads(text.stdout)
        metadata["source_kind"] = "tool"
        assert (
            client.post("/api/v1/human/interpret", json=metadata).json()["state"] == "quarantined"
        )
    templates = runner.invoke(cli, ["human", "templates"])
    assert templates.exit_code == 0
    assert len(json.loads(templates.stdout)["templates"]) == 4


def test_malformed_oversized_and_authority_metadata_are_rejected():
    service = HumanInterpreter(create_reference_ideon_runtime().controller)
    cli = typer.Typer()
    register_human_interpreter_commands(cli, lambda _: service)
    for payload in (
        '{"text":"a","text":"b"}',
        '{"text":"hello","grant":"admin"}',
        '{"text":"hello","received_at":"yesterday"}',
        "[]",
        "x" * 32769,
    ):
        result = CliRunner().invoke(cli, ["human", "interpret-json", payload])
        assert result.exit_code == 2, result.stdout
    result = CliRunner().invoke(cli, ["human", "interpret", "é" * 3000])
    assert result.exit_code == 2
    app = FastAPI()
    app.include_router(create_human_interpreter_router(service))
    with TestClient(app) as client:
        assert client.post("/api/v1/human/interpret", json={"text": "é" * 3000}).status_code == 422
        assert (
            client.post(
                "/api/v1/human/interpret", json={"text": "hello", "received_at": "2026-01-01"}
            ).status_code
            == 422
        )
