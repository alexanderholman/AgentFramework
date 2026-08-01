from __future__ import annotations

import json
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from af.interfaces.cli_interactions import PromptEvent, ScriptedCliInteraction
from af.interfaces.console import create_cli
from af.interfaces.http import create_http_app

runner = CliRunner()


def _load_generated_module(root: Path, relative_path: str, name: str):  # type: ignore[no-untyped-def]
    spec = spec_from_file_location(name, root / relative_path)
    assert spec is not None
    assert spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_procedure_scaffolds_a_runnable_provider_example(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    interactions: list[ScriptedCliInteraction] = []

    def build_interaction() -> ScriptedCliInteraction:
        interaction = ScriptedCliInteraction(
            text_responses=("uppercase", "generated_procedure_app"),
        )
        interactions.append(interaction)
        return interaction

    result = runner.invoke(create_cli(interaction_factory=build_interaction), ["make", "procedure"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    expected_files = [
        "generated_procedure_app/__init__.py",
        "generated_procedure_app/application.py",
        "generated_procedure_app/provider.py",
        "generated_procedure_app/procedure.py",
        "tests/__init__.py",
        "tests/test_uppercase_behavior.py",
        "tests/test_uppercase_contract.py",
        "tests/test_uppercase_failure.py",
        "tests/test_uppercase_unit.py",
    ]
    assert payload == {
        "procedure_id": "uppercase",
        "package": "generated_procedure_app",
        "application_factory": "generated_procedure_app.application:build_application",
        "root": str(tmp_path),
        "files": expected_files,
    }
    assert tuple(interactions[0].events) == (
        PromptEvent(kind="text", message="Procedure ID"),
        PromptEvent(kind="text", message="Python package"),
    )
    for relative in expected_files:
        assert (tmp_path / relative).is_file()

    generated = import_module("generated_procedure_app.application")
    application = generated.build_application()
    try:
        routes = runner.invoke(
            create_cli(),
            [
                "--application-factory",
                "generated_procedure_app.application:build_application",
                "routes",
            ],
        )
        invoked = runner.invoke(
            create_cli(),
            [
                "--application-factory",
                "generated_procedure_app.application:build_application",
                "ask",
                "uppercase hello world",
            ],
        )

        assert routes.exit_code == 0
        route_payload = json.loads(routes.stdout)
        uppercase = next(item for item in route_payload if item["procedure_id"] == "uppercase")
        assert uppercase["version"] == "1.0.0"
        assert uppercase["intent"] == "text.uppercase"
        assert uppercase["effect"] == "read_only"
        assert uppercase["risk"] == "none"
        assert uppercase["digest"].startswith("sha256:")
        assert invoked.exit_code == 0
        assert invoked.stdout == "HELLO WORLD\n"

        with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
            response = client.post("/api/v1/tasks", json={"prompt": "uppercase hello world"})

        assert response.status_code == 201
        assert response.json()["output"] == "HELLO WORLD"
        assert response.json()["route"] == "uppercase"
        assert response.json()["intent"] == "text.uppercase"
        assert response.json()["verified"] is True

        unit = _load_generated_module(tmp_path, "tests/test_uppercase_unit.py", "generated_unit")
        contract = _load_generated_module(
            tmp_path,
            "tests/test_uppercase_contract.py",
            "generated_contract",
        )
        behavior = _load_generated_module(
            tmp_path,
            "tests/test_uppercase_behavior.py",
            "generated_behavior",
        )
        failure = _load_generated_module(
            tmp_path,
            "tests/test_uppercase_failure.py",
            "generated_failure",
        )

        unit.test_classifier_extracts_text_argument()
        unit.test_procedure_uppercases_text()
        contract.test_routes_expose_stable_procedure_identity()
        behavior.test_uppercase_runs_through_cli_and_rest()
        failure.test_blank_payload_has_no_route()
        failure.test_verifier_failure_fails_closed()
    finally:
        application.shutdown()


def test_make_procedure_refuses_to_overwrite_existing_files(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    package_dir = tmp_path / "generated_procedure_app"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("fixture\n", encoding="utf-8")

    result = runner.invoke(
        create_cli(),
        [
            "make",
            "procedure",
            "--procedure-id",
            "uppercase",
            "--package",
            "generated_procedure_app",
        ],
    )

    assert result.exit_code == 2
    assert result.stdout == ""
    assert (
        result.stderr
        == "refusing to overwrite existing path: generated_procedure_app/__init__.py\n"
    )


def test_make_procedure_supports_valid_numeric_leading_procedure_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    package = "generated_procedure_app_2fa"

    result = runner.invoke(
        create_cli(),
        [
            "make",
            "procedure",
            "--procedure-id",
            "2fa",
            "--package",
            package,
        ],
    )

    assert result.exit_code == 0

    generated = import_module(f"{package}.application")
    application = generated.build_application()
    try:
        routes = runner.invoke(
            create_cli(),
            [
                "--application-factory",
                f"{package}.application:build_application",
                "routes",
            ],
        )
        invoked = runner.invoke(
            create_cli(),
            [
                "--application-factory",
                f"{package}.application:build_application",
                "ask",
                "2fa hello world",
            ],
        )

        assert routes.exit_code == 0
        route_payload = json.loads(routes.stdout)
        route = next(item for item in route_payload if item["procedure_id"] == "2fa")
        assert route["intent"] == "text.procedure_2fa"
        assert invoked.exit_code == 0
        assert invoked.stdout == "HELLO WORLD\n"

        with TestClient(create_http_app(application), base_url="http://127.0.0.1") as client:
            response = client.post("/api/v1/tasks", json={"prompt": "2fa hello world"})

        assert response.status_code == 201
        assert response.json()["intent"] == "text.procedure_2fa"
    finally:
        application.shutdown()
