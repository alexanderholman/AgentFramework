"""The exact remote disclosure and one-use consent are usable through CLI JSON."""

import json
from pathlib import Path

import httpx
import typer
from typer.testing import CliRunner

from af.application.decisions import DecisionController
from af.infrastructure.decision_consents import SQLiteDecisionConsentRegistry
from af.infrastructure.decision_providers import JevDecisionProvider
from af.interfaces.decisions import register_decision_commands


def test_cli_full_preview_approve_execute_receipt_revoke_path(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def factory(context: typer.Context) -> DecisionController:
        provider = JevDecisionProvider(
            api_key="secret-key",
            enabled=True,
            allow_remote=True,
            consents=SQLiteDecisionConsentRegistry(tmp_path / "consents.sqlite3"),
            transport=httpx.MockTransport(
                lambda request: (
                    calls.append(request)
                    or httpx.Response(
                        200,
                        json={
                            "model": "jev-1.13.0",
                            "answers": {"decision": {"type": "noul", "noul": 0.95}},
                            "usage": {"input_tokens": 10, "output_tokens": 1},
                        },
                    )
                )
            ),
        )
        return DecisionController(provider)

    cli = typer.Typer()
    register_decision_commands(cli, factory)
    runner = CliRunner()
    request = tmp_path / "request.json"
    preview = tmp_path / "preview.json"
    consent = tmp_path / "consent.json"
    request.write_text(
        json.dumps({"kind": "binary", "question": "Is it urgent?", "state": "Please help now."})
    )
    result = runner.invoke(cli, ["decisions", "schema"])
    assert result.exit_code == 0 and json.loads(result.stdout)["type"] == "object"
    result = runner.invoke(cli, ["decisions", "preview", "--request", str(request)])
    assert result.exit_code == 0, result.output
    preview.write_text(result.stdout)
    assert not calls
    result = runner.invoke(
        cli,
        [
            "decisions",
            "approve",
            "--request",
            str(request),
            "--preview",
            str(preview),
            "--max-cost-usd",
            "1",
            "--output",
            str(consent),
        ],
    )
    assert result.exit_code == 0, result.output
    assert consent.stat().st_mode & 0o777 == 0o600
    assert "token" not in result.stdout
    result = runner.invoke(
        cli, ["decisions", "evaluate", "--request", str(request), "--consent", str(consent)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["probability"] == 0.95
    receipt = runner.invoke(cli, ["decisions", "receipt", "--consent", str(consent)])
    assert receipt.exit_code == 0 and json.loads(receipt.stdout)["status"] == "answered"
    replay = runner.invoke(
        cli, ["decisions", "evaluate", "--request", str(request), "--consent", str(consent)]
    )
    assert replay.exit_code == 2 and json.loads(replay.stdout)["status"] == "denied"
    assert len(calls) == 1
    result = runner.invoke(cli, ["decisions", "revoke", "--consent", str(consent)])
    assert result.exit_code == 0
    assert "secret-key" not in result.output + receipt.output


def test_cli_approval_rejects_edited_disclosure_and_preserves_existing_output(
    tmp_path: Path,
) -> None:
    cli = typer.Typer()
    provider = JevDecisionProvider()
    register_decision_commands(cli, lambda context: DecisionController(provider))
    runner = CliRunner()
    request = tmp_path / "request.json"
    preview = tmp_path / "preview.json"
    output = tmp_path / "existing.json"
    request.write_text('{"kind":"binary","question":"?","state":"text"}')
    result = runner.invoke(cli, ["decisions", "preview", "--request", str(request)])
    data = json.loads(result.stdout)
    data["destination"] = "https://other-provider.example"
    preview.write_text(json.dumps(data))
    result = runner.invoke(
        cli,
        [
            "decisions",
            "approve",
            "--request",
            str(request),
            "--preview",
            str(preview),
            "--max-cost-usd",
            "1",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 2 and not output.exists()
    result = runner.invoke(cli, ["decisions", "preview", "--request", str(request)])
    preview.write_text(result.stdout)
    output.write_text("existing file")
    result = runner.invoke(
        cli,
        [
            "decisions",
            "approve",
            "--request",
            str(request),
            "--preview",
            str(preview),
            "--max-cost-usd",
            "1",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 2 and output.read_text() == "existing file"


def test_cli_malformed_input_and_missing_receipt_use_stable_exit_codes(tmp_path: Path) -> None:
    cli = typer.Typer()
    register_decision_commands(cli, lambda context: DecisionController(JevDecisionProvider()))
    runner = CliRunner()
    request = tmp_path / "invalid.json"
    for text in ('{"kind":"binary","kind":"choice"}', "[1,2]", "x" * 128001):
        request.write_text(text)
        result = runner.invoke(cli, ["decisions", "preview", "--request", str(request)])
        assert result.exit_code == 2
        assert json.loads(result.stderr)["error"] == "invalid_or_unavailable_decision_request"
    request.write_text('{"token":"unknown"}')
    result = runner.invoke(cli, ["decisions", "receipt", "--consent", str(request)])
    assert result.exit_code == 1 and json.loads(result.stdout) is None
